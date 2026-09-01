"""Observable WebSocket/REST transport state for production diagnostics.

The telemetry is deliberately observational: it does not change the existing
fallback/reconnect state machine. It distinguishes the two live trade paths:
1. Binance WebSocket/REST -> Redis EventBus
2. Binance WebSocket/REST -> direct live handler (bypass path)
"""

from __future__ import annotations

import contextvars
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger

logger = get_logger("aitos.data.transport_telemetry")

_MODE_UNKNOWN = "unknown"
_MODE_WEBSOCKET = "websocket"
_MODE_REST = "rest_fallback"
_SOURCE_WEBSOCKET = "websocket"
_SOURCE_REST = "rest_fallback"

_current_source: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aitos_trade_transport_source", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _install_telemetry(service: Any) -> None:
    if getattr(service, "_transport_telemetry_installed", False):
        return
    service._transport_telemetry_installed = True
    service._transport_mode = _MODE_UNKNOWN
    service._transport_fallback_count = 0
    service._transport_recovery_count = 0
    service._transport_rest_recovery_attempts = 0
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

    # Redis/EventBus path: count successful/failed writes separately from the
    # direct handler path so a degraded path cannot be hidden by the other.
    service._transport_ws_redis_events = 0
    service._transport_rest_redis_events = 0
    service._transport_ws_redis_errors = 0
    service._transport_rest_redis_errors = 0
    service._transport_ws_redis_last_event_at = None
    service._transport_rest_redis_last_event_at = None

    # Direct/bypass path: count handler successes/failures by source.
    service._transport_ws_direct_events = 0
    service._transport_rest_direct_events = 0
    service._transport_ws_direct_errors = 0
    service._transport_rest_direct_errors = 0
    service._transport_ws_direct_last_event_at = None
    service._transport_rest_direct_last_event_at = None


def _record_redis_event(service: Any, source: str, topic: str) -> None:
    now = _now_iso()
    if source == _SOURCE_REST:
        service._transport_rest_redis_events += 1
        service._transport_rest_redis_last_event_at = now
    else:
        service._transport_ws_redis_events += 1
        service._transport_ws_redis_last_event_at = now

    logger.debug(
        "trade transport Redis path event published",
        extra={
            "aitos_extra": {
                "transport_source": source,
                "downstream_path": "redis_eventbus",
                "topic": topic,
                "timestamp": now,
            }
        },
    )


def _record_redis_error(service: Any, source: str, topic: str, exc: Exception) -> None:
    if source == _SOURCE_REST:
        service._transport_rest_redis_errors += 1
    else:
        service._transport_ws_redis_errors += 1
    logger.error(
        "trade transport Redis path publish failed",
        extra={
            "aitos_extra": {
                "transport_source": source,
                "downstream_path": "redis_eventbus",
                "topic": topic,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        },
    )


def _wrap_event_bus_publish_once() -> None:
    if getattr(EventBus, "_transport_telemetry_publish_wrapped", False):
        return
    EventBus._transport_telemetry_publish_wrapped = True
    original_publish = EventBus.publish

    @wraps(original_publish)
    async def publish_wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> None:
        source = _current_source.get()
        if source is None:
            await original_publish(self, event, *args, **kwargs)
            return

        topic = getattr(event, "topic", "")
        is_trade_path_event = topic.startswith("market.trade.") or topic.startswith(
            "market.orderflow."
        )
        if not is_trade_path_event:
            await original_publish(self, event, *args, **kwargs)
            return

        try:
            await original_publish(self, event, *args, **kwargs)
        except Exception as exc:
            # The EventBus publish failure is surfaced unchanged to the caller.
            # Telemetry observes it without changing error semantics.
            owner = getattr(self, "_transport_telemetry_owner", None)
            if owner is not None:
                _record_redis_error(owner, source, topic, exc)
            raise
        else:
            owner = getattr(self, "_transport_telemetry_owner", None)
            if owner is not None:
                _record_redis_event(owner, source, topic)

    EventBus.publish = publish_wrapper


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


def _mark_direct_event(service: Any, source: str) -> None:
    now = _now_iso()
    if source == _SOURCE_REST:
        service._transport_rest_direct_events += 1
        service._transport_rest_direct_last_event_at = now
    else:
        service._transport_ws_direct_events += 1
        service._transport_ws_direct_last_event_at = now


def _mark_direct_error(service: Any, source: str, exc: Exception) -> None:
    if source == _SOURCE_REST:
        service._transport_rest_direct_errors += 1
    else:
        service._transport_ws_direct_errors += 1
    logger.error(
        "trade transport direct/bypass path failed",
        extra={
            "aitos_extra": {
                "transport_source": source,
                "downstream_path": "direct_live_handler",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        },
    )


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
        "transport_rest_recovery_attempts": service._transport_rest_recovery_attempts,
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
        "transport_ws_redis_events": service._transport_ws_redis_events,
        "transport_rest_redis_events": service._transport_rest_redis_events,
        "transport_ws_redis_errors": service._transport_ws_redis_errors,
        "transport_rest_redis_errors": service._transport_rest_redis_errors,
        "transport_ws_redis_last_event_at": service._transport_ws_redis_last_event_at,
        "transport_rest_redis_last_event_at": service._transport_rest_redis_last_event_at,
        "transport_ws_direct_events": service._transport_ws_direct_events,
        "transport_rest_direct_events": service._transport_rest_direct_events,
        "transport_ws_direct_errors": service._transport_ws_direct_errors,
        "transport_rest_direct_errors": service._transport_rest_direct_errors,
        "transport_ws_direct_last_event_at": service._transport_ws_direct_last_event_at,
        "transport_rest_direct_last_event_at": service._transport_rest_direct_last_event_at,
    }


def install_transport_telemetry(service_cls: type[Any]) -> None:
    """Install non-invasive telemetry on DataIngestionService."""
    _wrap_event_bus_publish_once()
    if getattr(service_cls, "_transport_telemetry_wrapped", False):
        return
    service_cls._transport_telemetry_wrapped = True

    original_init = service_cls.__init__

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _install_telemetry(self)
        # EventBus is shared, so the owner is explicitly attached to the
        # ingestion instance before each transport operation.
        self._event_bus._transport_telemetry_owner = self

        original_handler = self._live_trade_handler
        if original_handler is not None and not getattr(
            original_handler, "_transport_telemetry_wrapped", False
        ):

            @wraps(original_handler)
            async def direct_handler_wrapper(trade: Any) -> None:
                source = _current_source.get() or _SOURCE_WEBSOCKET
                try:
                    await original_handler(trade)
                except Exception as exc:
                    _mark_direct_error(self, source, exc)
                    raise
                else:
                    _mark_direct_event(self, source)

            direct_handler_wrapper._transport_telemetry_wrapped = True
            self._live_trade_handler = direct_handler_wrapper

    service_cls.__init__ = init_wrapper

    original_process = service_cls._process_trade_batch

    @wraps(original_process)
    async def process_wrapper(self: Any, trades: list[Any]) -> None:
        _install_telemetry(self)
        source = (
            _SOURCE_REST if self._transport_rest_recovery_active else _SOURCE_WEBSOCKET
        )
        token = _current_source.set(source)
        try:
            if source == _SOURCE_REST:
                _mark_rest_batch(self, len(trades))
            else:
                _mark_websocket_recovery(self)
            await original_process(self, trades)
        finally:
            _current_source.reset(token)

    service_cls._process_trade_batch = process_wrapper

    original_recover = service_cls._recover_recent_trades

    @wraps(original_recover)
    async def recover_wrapper(self: Any) -> None:
        _enter_rest_fallback(self, "websocket_idle_timeout")
        self._transport_rest_recovery_attempts += 1
        self._transport_rest_recovery_active = True
        try:
            await original_recover(self)
        except Exception:
            self._transport_rest_recovery_errors += 1
            raise
        finally:
            self._transport_rest_recovery_active = False

    service_cls._recover_recent_trades = recover_wrapper

    original_health = service_cls.health_check

    @wraps(original_health)
    async def health_wrapper(self: Any) -> Any:
        health = await original_health(self)
        health.details.update(_health_details(self))
        return health

    service_cls.health_check = health_wrapper


__all__ = ["install_transport_telemetry"]
