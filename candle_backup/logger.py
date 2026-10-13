"""Console + rotating file logging for the backup tool.

Both handlers share the same format so the console output matches
``logs/backup.log`` exactly.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from candle_backup.config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_FORMATTER = logging.Formatter(_LOG_FORMAT)

_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 3

_configured = False


def configure_logging() -> None:
    """Idempotently attach the console and file handlers to the root logger."""
    global _configured
    if _configured:
        return
    _configured = True

    settings.log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    if not any(getattr(h, "name", None) == "backup-console" for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(_FORMATTER)
        console.name = "backup-console"
        root.addHandler(console)

    if not any(getattr(h, "name", None) == "backup-file" for h in root.handlers):
        file_handler = RotatingFileHandler(
            settings.log_file,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(_FORMATTER)
        file_handler.name = "backup-file"
        root.addHandler(file_handler)

    logging.captureWarnings(True)


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)