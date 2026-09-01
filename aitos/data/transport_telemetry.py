"""Observable trade transport telemetry and stale REST recovery guard."""

from __future__ import annotations

import contextvars
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger

logger = get_logger("aitos.data.transport_telemetry")

MODE_UNKNOWN = "unknown"
MODE_WEBSOCKET = "websocket"
MODE_REST = "rest_fallback"
SOURCE_WEBSOCKET = "websocket"
SOURCE_REST = "rest_fallback"
REST_MAX_SOURCE_AGE_SECONDS = 15.0

_context: contextvars.ContextVar[tuple[Any, str] | None] = contextvars.ContextVar(
    "aitos_trade_transport_context", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _install(service: Any) -> None:
    if getattr(service, "_transport_telemetry_installed", False):
        return
    service._transport_telemetry_installed = True
    service._transport_mode = MODE_UNKNOWN
    service._transport_fallback_count = 0
    service._transport_recovery_count = 0
    service._transport_rest_recovery_attempts = 0
    service._transport_rest_recovery_errors = 0
    service._transport_ws_batches = 0
    service._transport_rest_batches = 0
    service._transport_rest_trades_recovered = 0
    service._transport_rest_stale_trades_filtered = 0
    service._transport_rest_last_batch_count = 0
    service._transport_rest_last_accepted_count = 0
    service._transport_rest_last_max_source_age_sec = None
    service._transport_rest_last_newest_trade_id = None
    service._transport_rest_last_oldest_trade_id = None
    service._transport_rest_last_newest_source_at = None
    service._transport_rest_last_oldest_source_at = None
    service._transport_last_ws_event_at = None
    service._transport_last_rest_event_at = None
    service._transport_last_fallback_started_at = None
    service._transport_last_recovery_at = None
    service._transport_fallback_active_since = None
    service._transport_fallback_total_seconds = 0.0
    service._transport_fallback_active_seconds = 0.0
    service._transport_rest_recovery_active = False
    service._transport_ws_by_symbol: dict[str, dict[str, Any]] = {}
    service._transport_rest_by_symbol: dict[str, dict[str, Any]] = {}
    service._transport_ws_redis_events = 0
    service._transport_rest_redis_events = 0
    service._transport_ws_redis_errors = 0
    service._transport_rest_redis_errors = 0
    service._transport_ws_redis_last_event_at = None
    service._transport_rest_redis_last_event_at = None
    service._transport_ws_direct_events = 0
    service._transport_rest_direct_events = 0
    service._transport_ws_direct_errors = 0
    service._transport_rest_direct_errors = 0
    service._transport_ws_direct_last_event_at = None
    service._transport_rest_direct_last_event_at = None


def _wrap_publish() -> None:
    if getattr(EventBus, "_transport_telemetry_publish_wrapped", False):
        return
    EventBus._transport_telemetry_publish_wrapped = True
    original = EventBus.publish

    @wraps(original)
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> None:
        context = _context.get()
        if context is None:
            await original(self, event, *args, **kwargs)
            return
        owner, source = context
        topic = getattr(event, "topic", "")
        if not (
            topic.startswith("market.trade.") or topic.startswith("market.orderflow.")
        ):
            await original(self, event, *args, **kwargs)
            return
        try:
            await original(self, event, *args, **kwargs)
        except Exception:
            key = (
                "_transport_rest_redis_errors"
                if source == SOURCE_REST
                else "_transport_ws_redis_errors"
            )
            setattr(owner, key, getattr(owner, key) + 1)
            raise
        else:
            key = (
                "_transport_rest_redis_events"
                if source == SOURCE_REST
                else "_transport_ws_redis_events"
            )
            setattr(owner, key, getattr(owner, key) + 1)
            now = _now_iso()
            setattr(
                owner,
                (
                    "_transport_rest_redis_last_event_at"
                    if source == SOURCE_REST
                    else "_transport_ws_redis_last_event_at"
                ),
                now,
            )

    EventBus.publish = wrapper


def _enter_rest(service: Any, reason: str) -> None:
    _install(service)
    if service._transport_mode == MODE_REST:
        return
    service._transport_mode = MODE_REST
    service._transport_fallback_count += 1
    service._transport_last_fallback_started_at = _now_iso()
    service._transport_fallback_active_since = time.monotonic()
    logger.warning(
        "trade transport switched to REST fallback",
        extra={
            "aitos_extra": {
                "reason": reason,
                "fallback_count": service._transport_fallback_count,
            }
        },
    )


def _record_ws_trade(service: Any, trade: Any) -> None:
    _install(service)
    now = _now_iso()
    symbol = str(getattr(trade, "symbol", "")).upper()
    source_at = getattr(trade, "timestamp", None)
    source_iso = source_at.isoformat() if source_at is not None else None
    service._transport_ws_by_symbol[symbol] = {
        "last_received_at": now,
        "last_source_at": source_iso,
        "last_trade_id": getattr(trade, "trade_id", None),
    }
    service._transport_last_ws_event_at = now


def _record_rest_trade(service: Any, trade: Any) -> None:
    _install(service)
    now = _now_iso()
    symbol = str(getattr(trade, "symbol", "")).upper()
    source_at = getattr(trade, "timestamp", None)
    service._transport_rest_by_symbol[symbol] = {
        "last_recovered_at": now,
        "last_source_at": source_at.isoformat() if source_at is not None else None,
        "last_trade_id": getattr(trade, "trade_id", None),
    }


def _filter_rest(service: Any, trades: list[Any]) -> list[Any]:
    _install(service)
    now = datetime.now(timezone.utc)
    ages: list[float] = []
    accepted: list[Any] = []
    ids: list[int] = []
    timestamps: list[datetime] = []
    for trade in trades:
        trade_id = getattr(trade, "trade_id", None)
        if isinstance(trade_id, int):
            ids.append(trade_id)
        timestamp = getattr(trade, "timestamp", None)
        if timestamp is None:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamps.append(timestamp)
        age = max(0.0, (now - timestamp).total_seconds())
        ages.append(age)
        if age <= REST_MAX_SOURCE_AGE_SECONDS:
            accepted.append(trade)
            _record_rest_trade(service, trade)

    service._transport_rest_last_batch_count = len(trades)
    service._transport_rest_last_accepted_count = len(accepted)
    service._transport_rest_last_max_source_age_sec = (
        round(max(ages), 3) if ages else None
    )
    service._transport_rest_last_newest_trade_id = max(ids) if ids else None
    service._transport_rest_last_oldest_trade_id = min(ids) if ids else None
    service._transport_rest_last_newest_source_at = (
        max(timestamps).isoformat() if timestamps else None
    )
    service._transport_rest_last_oldest_source_at = (
        min(timestamps).isoformat() if timestamps else None
    )
    filtered = len(trades) - len(accepted)
    service._transport_rest_stale_trades_filtered += filtered
    if filtered:
        logger.warning(
            "filtered stale REST trade recovery batch",
            extra={
                "aitos_extra": {
                    "batch_count": len(trades),
                    "accepted_count": len(accepted),
                    "filtered_count": filtered,
                    "max_source_age_sec": service._transport_rest_last_max_source_age_sec,
                    "newest_trade_id": service._transport_rest_last_newest_trade_id,
                    "oldest_trade_id": service._transport_rest_last_oldest_trade_id,
                }
            },
        )
    return accepted


def _health(service: Any) -> dict[str, Any]:
    _install(service)
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
        "transport_rest_stale_trades_filtered": service._transport_rest_stale_trades_filtered,
        "transport_rest_last_batch_count": service._transport_rest_last_batch_count,
        "transport_rest_last_accepted_count": service._transport_rest_last_accepted_count,
        "transport_rest_last_max_source_age_sec": service._transport_rest_last_max_source_age_sec,
        "transport_rest_last_newest_trade_id": service._transport_rest_last_newest_trade_id,
        "transport_rest_last_oldest_trade_id": service._transport_rest_last_oldest_trade_id,
        "transport_rest_last_newest_source_at": service._transport_rest_last_newest_source_at,
        "transport_rest_last_oldest_source_at": service._transport_rest_last_oldest_source_at,
        "transport_rest_max_source_age_sec": REST_MAX_SOURCE_AGE_SECONDS,
        "transport_last_ws_event_at": service._transport_last_ws_event_at,
        "transport_last_rest_event_at": service._transport_last_rest_event_at,
        "transport_last_fallback_started_at": service._transport_last_fallback_started_at,
        "transport_last_recovery_at": service._transport_last_recovery_at,
        "transport_fallback_active_seconds": round(active, 3),
        "transport_fallback_total_seconds": round(
            service._transport_fallback_total_seconds, 3
        ),
        "transport_ws_by_symbol": service._transport_ws_by_symbol,
        "transport_rest_by_symbol": service._transport_rest_by_symbol,
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
    """Install runtime trade transport telemetry without changing core stream logic."""
    _wrap_publish()
    if getattr(service_cls, "_transport_telemetry_wrapped", False):
        return
    service_cls._transport_telemetry_wrapped = True

    original_init = service_cls.__init__

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _install(self)

        original_handler = self._live_trade_handler
        if original_handler is not None and not getattr(
            original_handler, "_transport_telemetry_wrapped", False
        ):

            @wraps(original_handler)
            async def direct_wrapper(trade: Any) -> None:
                source = (
                    SOURCE_REST
                    if self._transport_rest_recovery_active
                    else SOURCE_WEBSOCKET
                )
                try:
                    await original_handler(trade)
                except Exception:
                    key = (
                        "_transport_rest_direct_errors"
                        if source == SOURCE_REST
                        else "_transport_ws_direct_errors"
                    )
                    setattr(self, key, getattr(self, key) + 1)
                    raise
                else:
                    key = (
                        "_transport_rest_direct_events"
                        if source == SOURCE_REST
                        else "_transport_ws_direct_events"
                    )
                    setattr(self, key, getattr(self, key) + 1)
                    setattr(
                        self,
                        (
                            "_transport_rest_direct_last_event_at"
                            if source == SOURCE_REST
                            else "_transport_ws_direct_last_event_at"
                        ),
                        _now_iso(),
                    )

            direct_wrapper._transport_telemetry_wrapped = True
            self._live_trade_handler = direct_wrapper

        # Observe the exchange iterator at the exact WS->ingestion boundary.
        exchange = self._exchange
        original_stream = exchange.stream_trades
        if not getattr(original_stream, "_transport_telemetry_wrapped", False):

            @wraps(original_stream)
            async def stream_wrapper(symbols: list[str]):
                async for trade in original_stream(symbols):
                    _record_ws_trade(self, trade)
                    yield trade

            stream_wrapper._transport_telemetry_wrapped = True
            exchange.stream_trades = stream_wrapper

    service_cls.__init__ = init_wrapper

    original_process = service_cls._process_trade_batch

    @wraps(original_process)
    async def process_wrapper(self: Any, trades: list[Any]) -> None:
        _install(self)
        source = (
            SOURCE_REST if self._transport_rest_recovery_active else SOURCE_WEBSOCKET
        )
        if source == SOURCE_REST:
            trades = _filter_rest(self, trades)
            if not trades:
                return
            self._transport_rest_batches += 1
            self._transport_rest_trades_recovered += len(trades)
            self._transport_last_rest_event_at = _now_iso()
        else:
            self._transport_ws_batches += 1
            self._transport_last_ws_event_at = _now_iso()
        token = _context.set((self, source))
        try:
            await original_process(self, trades)
        finally:
            _context.reset(token)

    service_cls._process_trade_batch = process_wrapper

    original_recover = service_cls._recover_recent_trades

    @wraps(original_recover)
    async def recover_wrapper(self: Any) -> None:
        _enter_rest(self, "websocket_idle_timeout")
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
        health.details.update(_health(self))
        return health

    service_cls.health_check = health_wrapper


__all__ = ["install_transport_telemetry"]
