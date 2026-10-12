"""File-based store for extracted invoice JSON - replaces the Postgres tables.

    STORAGE_DIR/pending/    extracted, not yet handed to the team
    STORAGE_DIR/delivered/  already returned by GET /api/v1/invoices/new (kept pdf_retention_days)
    STORAGE_DIR/failed/     attempt counters for PDFs that keep failing
    STORAGE_DIR/files/      the original PDF of each result, served by GET /api/v1/invoices/{id}/pdf
    STORAGE_DIR/jobs/       PDFs uploaded to POST /api/v1/invoices: <id>.processing while
                            extracting, then <id>.json (the result, served by GET /api/v1/invoices/{id})
    STORAGE_DIR/email_watermark.json   highest email UID already dealt with

A result's key is "{received_ts}_{hash(message_id)}_{n}", so it is the same on every poll
for the same email attachment: the poller skips any key that already exists in pending/ or
delivered/, and file names sort oldest-first.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from app.config import settings
from app.logging_conf import get_logger

logger = get_logger(__name__)

_root = Path(settings.storage_dir).resolve()
PENDING_DIR = _root / "pending"
DELIVERED_DIR = _root / "delivered"
FAILED_DIR = _root / "failed"
PDF_DIR = _root / "files"
JOBS_DIR = _root / "jobs"

for _dir in (PENDING_DIR, DELIVERED_DIR, FAILED_DIR, PDF_DIR, JOBS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


def result_key(message_id: str, received_at: datetime | None, index: int) -> str:
    received_at = received_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
    if received_at.tzinfo is not None:
        received_at = received_at.astimezone(timezone.utc)
    stamp = received_at.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:12]
    return f"{stamp}_{digest}_{index}"


def exists(key: str) -> bool:
    name = f"{key}.json"
    return (PENDING_DIR / name).exists() or (DELIVERED_DIR / name).exists()


def write_pending(key: str, data: dict) -> None:
    """Writes to a temp file first and renames it, so the API never reads a half-written
    file (claim_pending only picks up *.json)."""
    final_path = PENDING_DIR / f"{key}.json"
    tmp_path = PENDING_DIR / f"{key}.json.tmp"
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, final_path)
    logger.info("result_written", key=key, result=data)


def claim_pending() -> list[dict]:
    """Moves every pending result to delivered/ and returns their contents, oldest first.
    os.replace is atomic, so if two requests race, each file goes to exactly one of them."""
    results = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        target = DELIVERED_DIR / path.name
        try:
            os.replace(path, target)
        except FileNotFoundError:
            continue  # another request claimed it first
        try:
            result = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("result_unreadable", file=str(target))
            continue
        results.append(result)
        logger.info("result_delivered", key=path.stem, result=result)
    logger.info("results_claimed", count=len(results))
    return results


# Keys come from result_key(); anything else (e.g. "../") must never reach the filesystem.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def pdf_url(key: str) -> str:
    return f"/api/v1/invoices/{key}/pdf"


def save_pdf(key: str, content: bytes) -> None:
    """Keeps the original PDF for GET /api/v1/invoices/{id}/pdf, and deletes stored PDFs,
    upload jobs and delivered email results older than pdf_retention_days while at it."""
    tmp_path = PDF_DIR / f"{key}.pdf.tmp"
    tmp_path.write_bytes(content)
    os.replace(tmp_path, PDF_DIR / f"{key}.pdf")
    _delete_old_files()


def pdf_path(key: str) -> Path | None:
    if not _KEY_PATTERN.match(key):
        return None
    path = PDF_DIR / f"{key}.pdf"
    return path if path.is_file() else None


def _delete_old_files() -> None:
    if settings.pdf_retention_days <= 0:
        return
    cutoff = time.time() - settings.pdf_retention_days * 86400
    # pending/ is never cleaned: those results haven't been handed out yet. Deleting a
    # delivered result can't cause re-extraction - the email watermark already moved past it.
    for old in [*PDF_DIR.glob("*.pdf"), *JOBS_DIR.glob("*.json"), *DELIVERED_DIR.glob("*.json")]:
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def start_job(job_id: str) -> None:
    (JOBS_DIR / f"{job_id}.processing").touch()


def finish_job(job_id: str, data: dict) -> None:
    tmp_path = JOBS_DIR / f"{job_id}.json.tmp"
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, JOBS_DIR / f"{job_id}.json")
    (JOBS_DIR / f"{job_id}.processing").unlink(missing_ok=True)


def get_result(key: str) -> dict | None:
    """The finished result for an id - an uploaded job, or an email result (pending or
    delivered; looking it up doesn't claim it). None if there is no finished result."""
    if not _KEY_PATTERN.match(key):
        return None
    for folder in (JOBS_DIR, DELIVERED_DIR, PENDING_DIR):
        try:
            return json.loads((folder / f"{key}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return None


def job_started_at(job_id: str) -> float | None:
    """When an upload job still being extracted was started (epoch seconds), else None."""
    if not _KEY_PATTERN.match(job_id):
        return None
    try:
        return (JOBS_DIR / f"{job_id}.processing").stat().st_mtime
    except FileNotFoundError:
        return None


def pending_count() -> int:
    return sum(1 for _ in PENDING_DIR.glob("*.json"))


_WATERMARK_PATH = _root / "email_watermark.json"


def load_watermark() -> dict | None:
    """{"uid_validity": int, "last_uid": int} for the mail folder, or None on first run."""
    try:
        return json.loads(_WATERMARK_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def save_watermark(uid_validity: int, last_uid: int) -> None:
    tmp_path = _WATERMARK_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"uid_validity": uid_validity, "last_uid": last_uid}), encoding="utf-8")
    os.replace(tmp_path, _WATERMARK_PATH)


def bump_fail(key: str) -> int:
    """Records one more failed attempt for this key and returns the total so far."""
    path = FAILED_DIR / f"{key}.count"
    try:
        count = int(path.read_text(encoding="utf-8").strip() or 0)
    except (FileNotFoundError, ValueError):
        count = 0
    count += 1
    path.write_text(str(count), encoding="utf-8")
    return count


def clear_fail(key: str) -> None:
    try:
        (FAILED_DIR / f"{key}.count").unlink()
    except FileNotFoundError:
        pass
