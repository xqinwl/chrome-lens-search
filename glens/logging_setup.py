"""Structured, rotating logging configuration for the glens HTTP server."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MiB per file
DEFAULT_LOG_BACKUP_COUNT = 5

_HANDLER_TAG = "_glens_managed_handler"


class _UTCFormatter(logging.Formatter):
    converter = staticmethod(time.gmtime)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = self.converter(record.created)
        stamp = time.strftime(datefmt or "%Y-%m-%dT%H:%M:%S", ct)
        return f"{stamp}.{int(record.msecs):03d}Z"


def _parse_level(raw: str | None) -> int:
    name = (raw or DEFAULT_LOG_LEVEL).strip().upper()
    level = getattr(logging, name, None)
    return level if isinstance(level, int) else logging.INFO


def _parse_int(raw: str | None, default: int, minimum: int = 0) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return default


def configure_logging(
    *,
    level: str | None = None,
    log_file: str | Path | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> logging.Logger:
    """Configure the ``glens`` logger for stderr + optional rotating file output.

    Safe to call multiple times: replaces previously attached managed handlers
    rather than duplicating log lines.
    """
    resolved_level = _parse_level(
        level if level is not None else os.getenv("GLENS_LOG_LEVEL", DEFAULT_LOG_LEVEL)
    )
    resolved_file_raw = (
        str(log_file) if log_file is not None else os.getenv("GLENS_LOG_FILE", "")
    ).strip()
    resolved_max_bytes = (
        max_bytes
        if max_bytes is not None
        else _parse_int(os.getenv("GLENS_LOG_MAX_BYTES"), DEFAULT_LOG_MAX_BYTES, minimum=1024)
    )
    resolved_backups = (
        backup_count
        if backup_count is not None
        else _parse_int(
            os.getenv("GLENS_LOG_BACKUP_COUNT"), DEFAULT_LOG_BACKUP_COUNT, minimum=0
        )
    )

    formatter = _UTCFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    target = logging.getLogger("glens")
    target.setLevel(resolved_level)

    # Remove only our own previously managed handlers so external handlers (e.g. pytest) survive.
    for existing in list(target.handlers):
        if getattr(existing, _HANDLER_TAG, False):
            target.removeHandler(existing)
            try:
                existing.close()
            except Exception:
                pass

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(resolved_level)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _HANDLER_TAG, True)
    target.addHandler(stream_handler)

    if resolved_file_raw:
        try:
            path = Path(resolved_file_raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=resolved_max_bytes,
                backupCount=resolved_backups,
                encoding="utf-8",
            )
            file_handler.setLevel(resolved_level)
            file_handler.setFormatter(formatter)
            setattr(file_handler, _HANDLER_TAG, True)
            target.addHandler(file_handler)
        except OSError as exc:
            target.warning("glens: could not open log file %s (%s); logging to stderr only.",
                           resolved_file_raw, exc)

    # Avoid double-printing through root when we own the stderr handler.
    target.propagate = False
    return target
