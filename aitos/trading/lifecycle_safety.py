"""Idempotent market-price handling for benign closed-position races."""

from __future__ import annotations

from functools import wraps
from typing import Any

from aitos.logging_setup import get_logger

logger = get_logger("aitos.trading.lifecycle_safety")
_IDEMPOTENT_MARKERS = (
    "no remaining position to close",
    "position already closed",
    "trade is already closed",
)


def is_idempotent_closed_position_error(exc: BaseException) -> bool:
    message = str(exc).strip().lower()
    return any(marker in message for marker in _IDEMPOTENT_MARKERS)


def install_lifecycle_event_safety(lifecycle_cls: type[Any]) -> None:
    """Keep benign close/update races from becoming permanent DLQ entries.

    Only known idempotent closed-position errors are swallowed. Every other
    exception keeps the existing retry/DLQ semantics unchanged.
    """
    if getattr(lifecycle_cls, "_event_safety_installed", False):
        return
    lifecycle_cls._event_safety_installed = True
    original = lifecycle_cls.handle_event

    @wraps(original)
    async def guarded(self: Any, event: Any):
        try:
            return await original(self, event)
        except Exception as exc:
            if is_idempotent_closed_position_error(exc):
                logger.info(
                    "ignored idempotent market-event race for already-closed position",
                    extra={
                        "aitos_extra": {
                            "topic": getattr(event, "topic", ""),
                            "event_id": getattr(event, "event_id", None),
                            "reason": str(exc),
                        }
                    },
                )
                return None
            raise

    lifecycle_cls.handle_event = guarded


__all__ = ["install_lifecycle_event_safety", "is_idempotent_closed_position_error"]
