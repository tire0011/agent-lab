"""Shared stdlib and structured logging configuration for Nexus entrypoints."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from modules.experiment_logger import (
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_LOG_FILE_BYTES,
    DEFAULT_SYSTEM_LOG_BACKUP_COUNT,
    get_system_logger,
)

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_HANDLER_MARKER = "_nexus_central_handler"
_APPLICATION_LOGGERS = ("api", "mailbox_processor", "modules", "nexus")


def _configured_level() -> int:
    value = os.environ.get("NEXUS_LOG_LEVEL", "INFO").upper()
    if value == "TRACE":
        value = "DEBUG"
    return getattr(logging, value, logging.INFO)


class StructuredJsonlHandler(logging.Handler):
    """Mirror application stdlib records into the unified JSONL feed."""

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        *,
        max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
        backup_count: int = DEFAULT_SYSTEM_LOG_BACKUP_COUNT,
    ) -> None:
        super().__init__(level=_configured_level())
        self._structured = get_system_logger(
            log_dir=log_dir,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    def emit(self, record: logging.LogRecord) -> None:
        # ExperimentLogger already writes JSONL before mirroring to stdlib.
        if record.name == "nexus.experiment" or not record.name.startswith(_APPLICATION_LOGGERS):
            return
        try:
            level = record.levelname if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"
            phase = str(getattr(record, "phase", "") or record.name.rsplit(".", 1)[-1] or "system")
            event = str(getattr(record, "event", "") or record.funcName or "log")
            extra: dict[str, Any] = {}
            for field in ("experiment_id", "mailbox_task_id", "job_id", "session_id"):
                value = getattr(record, field, None)
                if value:
                    extra[field] = str(value)
            exc = record.exc_info[1] if record.exc_info else None
            if level == "ERROR":
                self._structured.error(phase, event, record.getMessage(), exc=exc, **extra)
            elif level == "WARNING":
                self._structured.warning(phase, event, record.getMessage(), **extra)
            elif level == "DEBUG":
                self._structured.debug(phase, event, record.getMessage(), **extra)
            else:
                self._structured.info(phase, event, record.getMessage(), **extra)
        except Exception:
            # Logging must never fail the application or echo an unsafe raw
            # record while reporting a secondary logging error.
            return


def configure_logging(log_dir: str | Path | None = None) -> None:
    """Install one consistent console and structured configuration, once."""
    root = logging.getLogger()
    level = _configured_level()
    root.setLevel(level)
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers):
        for handler in root.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                handler.setLevel(level)
        return

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    setattr(console, _HANDLER_MARKER, True)

    structured_dir = log_dir or os.environ.get("NEXUS_LOG_DIR") or DEFAULT_LOG_DIR
    structured = StructuredJsonlHandler(structured_dir)
    setattr(structured, _HANDLER_MARKER, True)
    root.addHandler(console)
    root.addHandler(structured)
    for noisy_logger in ("httpx", "httpx2"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
