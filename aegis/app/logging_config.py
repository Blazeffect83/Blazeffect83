"""Bounded, redacted logging (rotating file + stderr for journald)."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import Settings
from .security.redact import get_redactor


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        r = get_redactor()
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            msg = str(record.msg)
        record.msg = r.text(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = r.text(record.exc_text)
        return True


def setup_logging(settings: Settings, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    filt = RedactingFilter()

    settings.logs.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        settings.logs / "aegis.log",
        maxBytes=settings.log_max_mb * 1024 * 1024,
        backupCount=settings.log_backups,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.addFilter(filt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.addFilter(filt)
    root.addHandler(sh)
    root.setLevel(level)
    # Never log request headers from HTTP clients.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
