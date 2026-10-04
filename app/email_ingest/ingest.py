import html
import re

from imap_tools import AND, U, MailMessageFlags
from imap_tools.message import MailMessage

from app.config import settings
from app.email_ingest.client import open_mailbox
from app.logging_conf import get_logger
from app.pipeline.run import run_pipeline
from app.pipeline.to_response import build_failure, build_result
from app.schemas.envelope import EmailAttachment, EmailInfo
from app.storage import results_store

logger = get_logger(__name__)

# (uidvalidity, uid) of unread messages already found to have no PDF attachment. They stay
# unread in the mailbox, so while the watermark is held back behind an unfinished email,
# this stops every poll downloading them again.
_no_pdf_uids: set[tuple[int, int]] = set()


def _message_id(msg: MailMessage) -> str:
    raw = msg.headers.get("message-id")
    if raw:
        return raw[0]
    # Malformed/legacy senders occasionally omit Message-ID entirely - fall back to a key
    # that's still stable across polls for the same message (uid is stable within a folder
    # as long as the mailbox's UIDVALIDITY doesn't change), rather than skipping dedup.
    logger.warning("email_missing_message_id", uid=msg.uid, subject=msg.subject)
    return f"no-message-id:{settings.imap_folder}:{msg.uid}"


def allowed_senders() -> list[str]:
    return [s.strip().lower() for s in settings.imap_allowed_senders.split(",") if s.strip()]


def _sender_allowed(address: str | None) -> bool:
    allowed = allowed_senders()
    if not allowed:
        return True
    address = (address or "").strip().lower()
    domain = "@" + address.rsplit("@", 1)[-1] if "@" in address else None
    return address in allowed or (domain is not None and domain in allowed)


def _is_pdf_attachment(att) -> bool:
    # Checks the bytes, not the declared content type (senders often label PDFs
    # application/octet-stream), and tolerates a few junk bytes before the %PDF- header
    # the way PDF readers do - the old startswith() check silently skipped those.
    return b"%PDF-" in (att.payload or b"")[:1024]


def _strip_to_pdf_header(payload: bytes) -> bytes:
    return payload[payload.find(b"%PDF-"):]


_HTML_DROP_RE = re.compile(r"<(style|script|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _email_body(msg: MailMessage) -> str | None:
    """Plain-text body of the email - the text part, or the HTML part with tags stripped when
    the sender sent HTML only. Whitespace is collapsed and the result cut to email_body_max_chars."""
    text = (msg.text or "").strip()
    if not text and msg.html:
        text = _HTML_DROP_RE.sub(" ", msg.html)
        text = _HTML_BREAK_RE.sub("\n", text)
        text = html.unescape(_HTML_TAG_RE.sub(" ", text))
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[: settings.email_body_max_chars] or None


def _email_attachments(msg: MailMessage) -> list[EmailAttachment]:
    return [
        EmailAttachment(filename=att.filename or None, content_type=att.content_type or None,
                        size_bytes=len(att.payload or b""), is_pdf=_is_pdf_attachment(att))
        for att in msg.attachments
    ]


def _email_context(email: EmailInfo) -> str | None:
    """Subject + sender + body, as shown to the LLM next to the PDF text."""
    if not settings.email_context_in_prompt:
        return None
    parts = [f"Subject: {email.subject or ''}", f"From: {email.sender or ''}"]
    if email.body:
        parts += ["", email.body]
    return "\n".join(parts)


def _extract_attachment(key: str, attachment, email: EmailInfo, log) -> bool:
    """Runs the pipeline on one PDF and writes its result to pending/. Returns True once
    this attachment has a result file (success, needs_review, or a final failure), False if
    it failed and should be retried on the next poll."""
    pdf_bytes = _strip_to_pdf_header(attachment.payload)
    results_store.save_pdf(key, pdf_bytes)
    try:
        result = run_pipeline(pdf_bytes, key, email_context=_email_context(email))
    except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the rest
        attempts = results_store.bump_fail(key)
        log.exception("pdf_extraction_failed", key=key, filename=attachment.filename, attempt=attempts)
        if attempts < settings.email_max_attempts:
            return False
        # Out of attempts - hand the team a "failed" result instead of retrying forever.
        failure = build_failure(key, f"{type(exc).__name__}: {exc}", attachment.filename, email)
        failure.pdf_url = results_store.pdf_url(key)
        results_store.write_pending(key, failure.model_dump(mode="json"))
        results_store.clear_fail(key)
        return True

    item = build_result(key, result, attachment.filename, email)
    item.pdf_url = results_store.pdf_url(key)
    results_store.write_pending(key, item.model_dump(mode="json"))
    results_store.register_hash(pdf_bytes, key)
    results_store.clear_fail(key)
    log.info("pdf_extracted", key=key, filename=attachment.filename, status=result["status"])
    _push_to_sap(key, pdf_bytes, attachment.filename, email, log)
    return True


def _push_to_sap(key: str, pdf_bytes: bytes, filename: str | None, email: EmailInfo, log) -> None:
    """Hands the PDF to SAP (RFC ZFTVIA_PROCESS_DOCUMENT) when SAP_PUSH_ENABLED. A failed
    push is logged, not fatal: the result stays in pending/ for GET /api/v1/invoices/new."""
    if not settings.sap_push_enabled:
        return
    try:
        from app.sap.push import push_document

        push_document(key, pdf_bytes, filename, source_ref=f"{email.message_id}#{key}")
    except Exception:  # noqa: BLE001 - SAP being down must not stop email ingestion
        log.exception("sap_push_failed", key=key)


def _handle_message(msg: MailMessage) -> bool | None:
    """Extracts every PDF attachment of one email. Returns None if it is skipped (sender not
    in imap_allowed_senders, or no PDF), True if every PDF now has a result (so the email can
    be marked read), False otherwise."""
    log = logger.bind(uid=msg.uid, subject=msg.subject, from_=msg.from_)

    if not _sender_allowed(msg.from_):
        log.warning("email_skipped_sender_not_allowed")
        return None

    pdf_attachments = [att for att in msg.attachments if _is_pdf_attachment(att)]
    if not pdf_attachments:
        log.info(
            "email_skipped_no_pdf_attachment",
            attachments=[(att.filename, att.content_type) for att in msg.attachments],
        )
        return None

    message_id = _message_id(msg)
    received_at = msg.date if msg.date.year > 1900 else None  # imap_tools uses 1900-01-01 when missing
    email = EmailInfo(message_id=message_id, sender=msg.from_, subject=msg.subject, received_at=received_at,
                      body=_email_body(msg), attachments=_email_attachments(msg))

    all_done = True
    for index, attachment in enumerate(pdf_attachments, start=1):
        key = results_store.result_key(message_id, received_at, index)
        if results_store.exists(key):
            # Extracted on an earlier poll that didn't get as far as marking the email read.
            log.info("pdf_already_extracted", key=key)
            continue
        if not _extract_attachment(key, attachment, email, log):
            all_done = False
    return all_done


def _load_watermark(mailbox) -> tuple[int, int]:
    """Returns (uid_validity, last_uid): the highest UID in imap_folder already dealt with.
    The first time (or after the folder's UIDVALIDITY changes, which invalidates every
    stored UID) it starts at the folder's current highest UID - so only mail arriving from
    then on is extracted, and the existing unread backlog is ignored without downloading it."""
    status = mailbox.folder.status(settings.imap_folder, ("UIDNEXT", "UIDVALIDITY"))
    uid_validity, current_max_uid = status["UIDVALIDITY"], status["UIDNEXT"] - 1

    stored = results_store.load_watermark()
    if stored is not None and stored.get("uid_validity") == uid_validity:
        return uid_validity, stored["last_uid"]

    if stored is None:
        logger.info("email_watermark_initialized", folder=settings.imap_folder, last_uid=current_max_uid)
    else:
        logger.warning(
            "email_uidvalidity_changed",
            folder=settings.imap_folder,
            old_uid_validity=stored.get("uid_validity"),
            new_uid_validity=uid_validity,
            last_uid=current_max_uid,
        )
    results_store.save_watermark(uid_validity, current_max_uid)
    return uid_validity, current_max_uid


def run_ingest_cycle() -> None:
    """One poll: every unread email in imap_folder that arrived after the watermark (see
    _load_watermark) and has PDF attachment(s) is extracted to JSON files in pending/ (see
    app/storage/results_store.py). An email is marked read only once all of its PDFs have a
    result; otherwise it stays unread and is retried next poll. Emails without a PDF are
    left unread and untouched.

    Two IMAP logins at most per poll: one to list and download the new messages, one to
    mark the finished ones read. The connection is not held open while PDFs are extracted
    (that can take minutes and servers drop idle connections)."""
    extracted = skipped_no_pdf = incomplete = failed = 0

    # --- 1. list + download new unread messages ---
    with open_mailbox() as mailbox:
        uid_validity, last_uid = _load_watermark(mailbox)
        # "N:*" always matches the folder's highest UID even when it's below N (IMAP quirk),
        # hence the explicit > filter.
        new_uids = sorted(
            int(uid)
            for uid in mailbox.uids(AND(seen=False, uid=U(str(last_uid + 1), "*")))
            if int(uid) > last_uid
        )
        todo = [uid for uid in new_uids if (uid_validity, uid) not in _no_pdf_uids]
        messages = []
        if todo:
            messages = sorted(
                mailbox.fetch(AND(uid=[str(uid) for uid in todo]), mark_seen=False),
                key=lambda m: int(m.uid),
            )

    # --- 2. extract PDFs (no IMAP connection open) ---
    done: set[int] = {uid for uid in new_uids if (uid_validity, uid) in _no_pdf_uids}
    to_mark_read: list[str] = []
    for msg in messages:
        try:
            outcome = _handle_message(msg)
        except Exception:  # noqa: BLE001 - one bad message must not abort the whole cycle
            failed += 1
            logger.exception("email_ingest_message_failed", uid=msg.uid)
            continue
        if outcome is None:
            _no_pdf_uids.add((uid_validity, int(msg.uid)))
            done.add(int(msg.uid))
            skipped_no_pdf += 1
        elif outcome:
            to_mark_read.append(msg.uid)
        else:
            incomplete += 1

    # --- 3. mark finished emails read, then advance the watermark ---
    if to_mark_read:
        with open_mailbox() as mailbox:
            mailbox.flag(to_mark_read, MailMessageFlags.SEEN, True)
            if settings.imap_processed_folder:
                mailbox.move(to_mark_read, settings.imap_processed_folder)
        done.update(int(uid) for uid in to_mark_read)
        extracted = len(to_mark_read)

    # The watermark stops just before the first email that isn't finished, so that one is
    # retried next poll. Finished emails after it are read (or cached as no-PDF), so they
    # aren't picked up again - and results_store.exists() dedups them regardless.
    new_watermark = last_uid
    for uid in new_uids:
        if uid not in done:
            break
        new_watermark = uid
    if new_watermark > last_uid:
        results_store.save_watermark(uid_validity, new_watermark)

    # Logged every cycle (not just when something is found) so a quiet log unambiguously
    # means "polled, nothing new" rather than "ingester is stuck or not running".
    logger.info(
        "email_ingest_cycle_complete",
        new_unread=len(new_uids),
        extracted=extracted,
        skipped_no_pdf=skipped_no_pdf,
        retry_next_poll=incomplete,
        failed=failed,
        last_uid=new_watermark,
        pending_results=results_store.pending_count(),
    )
