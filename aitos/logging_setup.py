"""Structured JSON logging for AITOS.

Every log line is machine-readable and suitable for centralized ingestion.
High-frequency paths should prefer DEBUG or sampled/periodic INFO logging.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict


SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "secret",
    "password",
    "token",
    "authorization",
}


def _safe_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Redact credential-like context before it reaches the log sink."""
    safe: Dict[str, Any] = {}
    for key, value in context.items():
        safe[key] = "[REDACTED]" if key.lower() in SENSITIVE_KEYS else value
    return safe


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
        }
        extra = getattr(record, "aitos_extra", None)
        if extra:
            payload.update(_safe_context(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with_context(
    logger: logging.Logger, level: int, message: str, **context: Any
) -> None:
    logger.log(level, message, extra={"aitos_extra": _safe_context(context)})


def log_exception(logger: logging.Logger, message: str, **context: Any) -> None:
    """Log an exception with structured, credential-safe context."""
    logger.error(
        message,
        exc_info=True,
        extra={"aitos_extra": _safe_context(context)},
    )
