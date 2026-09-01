"""Observable WebSocket/REST trade transport state for production diagnostics.

This module deliberately observes the existing ingestion state machine without
changing its fallback/reconnect behavior. It records explicit transport
transitions so production audits can distinguish a WebSocket gap, REST
fallback, and successful WebSocket recovery.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

from aitos.logging_setup import get_logger

logger = get_logger("aitos.data.transport_telemetry")


_MODE_UNKNOWN = "unknown"
_MODE_WEBSOCKET = "websocket"
_MODE_REST = "rest_fallback"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _install_telemetry(service: Any) -> None:
    if getattr(service, "_transport_telemetry_installed", False):
        return
    service._transport_telemetry_installed = True
    service._transport_mode = _MODE_UNKNOWN
    service._transport_fallback_count = 0
    service._transport_recovery_count = 0
    service._transport_rest_recovery_errors = 0
    service._transport_ws_batches = 0
    service._transport_rest_batches = 0
    service._transport_rest_trades_recovered = 0
    service._transport_last_ws_event_at = None
    service._transport_last_rest_event_at = None
    service._transport_last_fallback_started_at = None
    service._transport_last_recovery_at = None
    service._transport_fallback_active_since = None
    service._transport_fallback_total_seconds = 0.0
    service._transport_fallback_active_seconds = 0.0
    service._transport_rest_recovery_active = False


def _enter_rest_fallback(service: Any, reason: str) -> None:
    _install_telemetry(service)
    if service._transport_mode == _MODE_REST:
        return
    now = _now_iso()
    service._transport_mode = _MODE_REST
    service._transport_fallback_count += 1
    service._transport_last_fallback_started_at = now
    service._transport_fallback_active_since = time.monotonic()
    logger.warning(
        "trade transport switched to REST fallback",
        extra={
            "aitos_extra": {
                "transport_mode": _MODE_REST,
                "transition": "websocket_to_rest",
                "reason": reason,
                "fallback_count": service._transport_fallback_count,
                "timestamp": now,
            }
        },
    )


def _mark_websocket_recovery(service: Any) -> None:
    _install_telemetry(service)
    now = _now_iso()
    service._transport_ws_batches += 1
    service._transport_last_ws_event_at = now
    if service._transport_mode != _MODE_REST:
        service._transport_mode = _MODE_WEBSOCKET
        return

    duration = 0.0
    if service._transport_fallback_active_since is not None:
        duration = max(0.0, time.monotonic() - service._transport_fallback_active_since)
    service._transport_fallback_total_seconds += duration
    service._transport_fallback_active_seconds = 0.0
    service._transport_fallback_active_since = None
    service._transport_recovery_count += 1
    service._transport_last_recovery_at = now
    service._transport_mode = _MODE_WEBSOCKET
    logger.info(
        "trade transport recovered to WebSocket",
        extra={
            "aitos_extra": {
                "transport_mode": _MODE_WEBSOCKET,
                "transition": "rest_to_websocket",
                "recovery_count": service._transport_recovery_count,
                "fallback_duration_seconds": round(duration, 3),
                "timestamp": now,
            }
        },
    )


def _mark_rest_batch(service: Any, trade_count: int) -> None:
    _install_telemetry(service)
    service._transport_rest_batches += 1
    service._transport_rest_trades_recovered += trade_count
    service._transport_last_rest_event_at = _now_iso()


def _health_details(service: Any) -> dict[str, Any]:
    _install_telemetry(service)
    active = 0.0
    if service._transport_fallback_active_since is not None:
        active = max(0.0, time.monotonic() - service._transport_fallback_active_since)
    service._transport_fallback_active_seconds = active
    return {
        "transport_mode": service._transport_mode,
        "transport_fallback_count": service._transport_fallback_count,
        "transport_recovery_count": service._transport_recovery_count,
        "transport_rest_recovery_errors": service._transport_rest_recovery_errors,
        "transport_ws_batches": service._transport_ws_batches,
        "transport_rest_batches": service._transport_rest_batches,
        "transport_rest_trades_recovered": service._transport_rest_trades_recovered,
        "transport_last_ws_event_at": service._transport_last_ws_event_at,
        "transport_last_rest_event_at": service._transport_last_rest_event_at,
        "transport_last_fallback_started_at": service._transport_last_fallback_started_at,
        "transport_last_recovery_at": service._transport_last_recovery_at,
        "transport_fallback_active_seconds": round(active, 3),
        "transport_fallback_total_seconds": round(
            service._transport_fallback_total_seconds, 3
        ),
    }


def install_transport_telemetry(service_cls: type[Any]) -> None:
    """Install non-invasive telemetry wrappers once on DataIngestionService."""
    if getattr(service_cls, "_transport_telemetry_wrapped", False):
        return
    service_cls._transport_telemetry_wrapped = True

    original_init = service_cls.__init__

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _install_telemetry(self)

    service_cls.__init__ = init_wrapper

    original_process = service_cls._process_trade_batch

    @wraps(original_process)
    async def process_wrapper(self: Any, trades: list[Any]) -> None:
        _install_telemetry(self)
        if self._transport_rest_recovery_active:
            _mark_rest_batch(self, len(trades))
        else:
            _mark_websocket_recovery(self)
        await original_process(self, trades)

    service_cls._process_trade_batch = process_wrapper

    original_recover = service_cls._recover_recent_trades

    @wraps(original_recover)
    async def recover_wrapper(self: Any) -> None:
        _enter_rest_fallback(self, "websocket_idle_timeout")
        self._transport_rest_recovery_active = True
        before_errors = self._transport_rest_recovery_errors
        try:
            await original_recover(self)
        except Exception:
            self._transport_rest_recovery_errors += 1
            raise
        finally:
            self._transport_rest_recovery_active = False
            # The underlying recovery method handles per-symbol failures itself.
            # Its stream-error counter is retained; this telemetry counter is only
            # for unexpected exceptions escaping the recovery method.
            if self._transport_rest_recovery_errors != before_errors:
                logger.exception("unexpected REST recovery exception")

    service_cls._recover_recent_trades = recover_wrapper

    original_health = service_cls.health_check

    @wraps(original_health)
    async def health_wrapper(self: Any) -> Any:
        health = await original_health(self)
        health.details.update(_health_details(self))
        return health

    service_cls.health_check = health_wrapper


__all__ = ["install_transport_telemetry"]
