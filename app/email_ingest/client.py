from imap_tools import MailBox, MailBoxUnencrypted

from app.config import settings


def open_mailbox() -> MailBox:
    """Connects and logs into the configured IMAP mailbox, selecting imap_folder. Returned
    box is a context manager (see run_ingest_cycle) - closing it logs out cleanly."""
    box_cls = MailBox if settings.imap_use_ssl else MailBoxUnencrypted
    # Without a timeout a socket Gmail silently dropped (seen as "EOF occurred in violation
    # of protocol") can block the ingest loop indefinitely instead of failing the cycle.
    box = box_cls(settings.imap_host, settings.imap_port, timeout=settings.imap_timeout_seconds)
    box.login(settings.imap_username, settings.imap_password, initial_folder=settings.imap_folder)
    return box
