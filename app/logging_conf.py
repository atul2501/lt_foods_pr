import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import structlog
from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_NAME = "app.log"


def resolve_log_path(log_file: str) -> Path:
    """LOG_FILE may be a file ("logs/app.log") or just the folder ("logs", "./logs/") - a
    folder gets app.log inside it, so the daily files always land in that folder rather
    than next to it as "logs-<date>". Relative paths are anchored to the project root, not
    the current directory, so logs end up in the same logs/ folder however the app is started."""
    path = Path(log_file).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.is_dir() or not path.suffix or log_file.endswith(("/", "\\")):
        path = path / DEFAULT_LOG_NAME
    return path


@contextmanager
def _interprocess_lock(lock_path: Path):
    """Exclusive lock shared by every process logging to the same file. Without it the API's
    and email poller's lines get lost or interleaved on Windows, where append mode is a
    seek-to-end followed by a write, not one atomic step."""
    with open(lock_path, "a+") as f:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


class DailyFileHandler(logging.Handler):
    """Today's lines go to LOG_FILE itself ("logs/app.log"); on the first write of a new day
    the previous day's file is moved aside as "logs/app-2026-09-25.log", and dated files
    older than log_retention_days are deleted. The day a file belongs to is its last-modified
    date, so a service that was stopped over several days still files it under the right date.

    The API and email poller processes both append to the same app.log, so every write (and
    the move) happens under a lock shared between processes (app.log.lock), and the file is
    opened per write rather than held open - on Windows a file can't be renamed while another
    process has it open. If the rename still fails (say an editor has it open), its contents
    are copied to the dated file and app.log is emptied instead."""

    def __init__(self, base_path: Path, retention_days: int):
        super().__init__()
        self._base = base_path
        self._retention_days = retention_days
        self._day: date | None = None
        self._lock_path = base_path.with_name(base_path.name + ".lock")
        base_path.parent.mkdir(parents=True, exist_ok=True)

    def _path_for(self, day: date) -> Path:
        return self._base.with_name(f"{self._base.stem}-{day.isoformat()}{self._base.suffix}")

    def _roll_if_new_day(self) -> None:
        today = date.today()
        if today == self._day:
            return
        self._archive_if_stale(today)
        self._day = today
        self._delete_old_files()

    def _archive_if_stale(self, today: date) -> None:
        try:
            last_written = date.fromtimestamp(self._base.stat().st_mtime)
        except FileNotFoundError:
            return
        if last_written >= today:
            return  # already today's file - the other process moved yesterday's aside first
        target = self._path_for(last_written)
        for _ in range(5):
            if not target.exists():
                try:
                    self._base.rename(target)
                    return
                except FileNotFoundError:
                    return  # the other process just moved it
                except PermissionError:
                    time.sleep(0.05)  # the other process is mid-write - try again
            else:
                break
        # Rename kept failing (or that date's file already exists): append to it and empty app.log.
        try:
            with open(self._base, "r+", encoding="utf-8") as src:
                with open(target, "a", encoding="utf-8") as dst:
                    dst.write(src.read())
                src.seek(0)
                src.truncate()
        except FileNotFoundError:
            pass

    def _delete_old_files(self) -> None:
        cutoff = time.time() - self._retention_days * 86400
        for old in self._base.parent.glob(f"{self._base.stem}-*{self._base.suffix}"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass  # already deleted by the other process, or still open - next day retries

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.lock, _interprocess_lock(self._lock_path):
                self._roll_if_new_day()
                with open(self._base, "a", encoding="utf-8") as stream:
                    stream.write(self.format(record) + "\n")
        except Exception:  # noqa: BLE001 - logging must never crash the app
            self.handleError(record)


def configure_logging() -> None:
    """Logs JSON lines to stdout (journalctl under systemd) and, when LOG_FILE is set, also
    to a daily file on disk (see DailyFileHandler) - every line already carries an ISO timestamp (structlog's TimeStamper)
    and every pipeline/task stage logs its own start/complete event, so the file is a full
    start-to-end, timestamped trace per job without any extra work at the call site.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if settings.log_file:
        file_handler = DailyFileHandler(resolve_log_path(settings.log_file), settings.log_retention_days)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str = "invoice_service"):
    return structlog.get_logger(name)
