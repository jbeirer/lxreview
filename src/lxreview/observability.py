"""Rotating package logs; credentials and hidden reasoning never reach the formatter."""

import json
import logging
from logging.handlers import RotatingFileHandler

from .paths import Paths, atomic_write
from .security import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
            }
        )


def configure(paths: Paths, debug: bool = False) -> None:
    if not paths.config.exists():
        return
    logger = logging.getLogger("lxreview")
    logger.handlers.clear()
    logfile = paths.root / "logs/lxreview.jsonl"
    if logfile.is_symlink():
        return
    if not logfile.exists():
        atomic_write(logfile, "")
    logfile.chmod(0o600)
    handler = RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(RedactingFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
