"""Rotating file logs with secret/account redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|authorization|bearer|api[_-]?key|account|acctid)\s*[=:]\s*\S+"
)
_ACCOUNT_RE = re.compile(r"\bU\d{5,}\b")  # typical IBKR account pattern; drop if present


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_RE.sub(r"\1=***", msg)
        redacted = _ACCOUNT_RE.sub("[redacted]", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(log_dir: Path, *, name: str = "ibkr_collector") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir / "collector.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(_RedactFilter())
    logger.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    stream.addFilter(_RedactFilter())
    logger.addHandler(stream)
    logging.getLogger("ibkr_collector").addFilter(_RedactFilter())
    logging.getLogger("ibapi").setLevel(logging.WARNING)
    return logger
