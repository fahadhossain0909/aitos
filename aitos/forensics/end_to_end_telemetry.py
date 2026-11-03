"""Non-invasive market-data pipeline telemetry.

Tracks source age and local stage latency without changing event semantics.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from functools import wraps
from typing import Any

MARKET_PREFIXES = (
    "market.trade.",
    "market.orderbook.",
    "market.orderflow.",
    "market.liquidity.",
    "market.live_state.",
    "market.kline.",
)


def _market(topic: str) -> bool:
    return topic.startswith(MARKET_PREFIXES)


def _key(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    symbol = payload.get("symbol") or payload.get("s")
    trade_id = payload.get("trade_id")
    return (
        f"{symbol}:{trade_id}" if symbol is not None and trade_id is not None else None
    )


def _stats_add(stats: dict[str, dict[str, float]], key: str, ms: float) -> None:
    row = stats.setdefault(key, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
    row["count"] += 1
    row["total_ms"] += ms
    row["max_ms"] = max(row["max_ms"], ms)


def install(eventbus_cls: type[Any]) -> None:
    """Install source, parser, publish, consumer and event-loop telemetry."""
    if getattr(eventbus_cls, "_e2e_telemetry_installed", False):
        return
    eventbus_cls._e2e_telemetry_installed = True
    original_init = eventbus_cls.__init__
    original_publish = eventbus_cls.publish
    original_process = eventbus_cls._process_message
    original_health = eventbus_cls.health_check
    original_shutdown = eventbus_cls.shutdown

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._e2e_publish_stats: dict[str, dict[str, float]] = {}
        self._e2e_handler_stats: dict[str, dict[str, float]] = {}
        self._e2e_recent: deque[dict[str, Any]] = deque(maxlen=100)
        self._e2e_loop_lag_ms = 0.0
        self._e2e_loop_lag_max_ms = 0.0
        self._e2e_loop_samples = 0
        try:
            self._e2e_watchdog_task = asyncio.create_task(_watchdog(self))
        except RuntimeError:
            self._e2e_watchdog_task = None

    @wraps(original_publish)
    async def publish(self: Any, event: Any, *args: Any, **kwargs: Any) -> None:
        topic = getattr(event, "topic", "")
        if not _market(topic):
            await original_publish(self, event, *args, **kwargs)
            return
        started = time.perf_counter()
        payload = getattr(event, "payload", {})
        trace_key = _key(payload)
        if trace_key:
            self._e2e_recent.append(
                {"trace_key": trace_key, "stage": "publish_start", "ts": time.time()}
            )
        try:
            await original_publish(self, event, *args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            _stats_add(self._e2e_publish_stats, topic, elapsed)
            if trace_key:
                self._e2e_recent.append(
                    {
                        "trace_key": trace_key,
                        "stage": "publish_end",
                        "publish_ms": round(elapsed, 3),
                        "ts": time.time(),
                    }
                )

    @wraps(original_process)
    async def process(
        self: Any,
        stream_key: Any,
        entry_id: Any,
        fields: dict[str, Any],
        group: str,
        handler: Any,
    ) -> None:
        try:
            from aitos.core.contracts import Event

            event = Event.from_wire(fields)
            topic = event.topic
            payload = event.payload
        except Exception:
            topic, payload = "", {}
        if not _market(topic):
            await original_process(self, stream_key, entry_id, fields, group, handler)
            return
        started = time.perf_counter()
        try:
            await original_process(self, stream_key, entry_id, fields, group, handler)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            _stats_add(self._e2e_handler_stats, topic, elapsed)
            trace_key = _key(payload)
            if trace_key:
                self._e2e_recent.append(
                    {
                        "trace_key": trace_key,
                        "stage": "consumer_end",
                        "group": group,
                        "entry_id": str(entry_id),
                        "handler_ms": round(elapsed, 3),
                        "ts": time.time(),
                    }
                )

    @wraps(original_health)
    async def health(self: Any):
        from dataclasses import replace

        status = await original_health(self)
        details = dict(status.details)
        details["market_data_e2e"] = {
            "publish_latency": _format(self._e2e_publish_stats),
            "handler_latency": _format(self._e2e_handler_stats),
            "event_loop": {
                "samples": self._e2e_loop_samples,
                "last_lag_ms": round(self._e2e_loop_lag_ms, 3),
                "max_lag_ms": round(self._e2e_loop_lag_max_ms, 3),
            },
            "recent_traces": list(self._e2e_recent),
        }
        return replace(status, details=details)

    @wraps(original_shutdown)
    async def shutdown(self: Any, *args: Any, **kwargs: Any) -> None:
        task = getattr(self, "_e2e_watchdog_task", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await original_shutdown(self, *args, **kwargs)

    eventbus_cls.__init__ = init
    eventbus_cls.publish = publish
    eventbus_cls._process_message = process
    eventbus_cls.health_check = health
    eventbus_cls.shutdown = shutdown
    _install_parser_wrappers()


def _format(stats: dict[str, dict[str, float]]) -> dict[str, dict[str, float | int]]:
    return {
        key: {
            "count": int(row["count"]),
            "total_ms": round(row["total_ms"], 3),
            "avg_ms": round(row["total_ms"] / row["count"], 3) if row["count"] else 0.0,
            "max_ms": round(row["max_ms"], 3),
        }
        for key, row in sorted(stats.items())
    }


async def _watchdog(eventbus: Any) -> None:
    loop = asyncio.get_running_loop()
    interval = 1.0
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        lag = max(0.0, (loop.time() - expected) * 1000)
        eventbus._e2e_loop_samples += 1
        eventbus._e2e_loop_lag_ms = lag
        eventbus._e2e_loop_lag_max_ms = max(eventbus._e2e_loop_lag_max_ms, lag)
        if lag >= 100:
            from aitos.logging_setup import get_logger

            get_logger("aitos.forensics.e2e").warning(
                "event-loop scheduling lag",
                extra={"aitos_extra": {"lag_ms": round(lag, 3)}},
            )


def _install_parser_wrappers() -> None:
    try:
        from aitos.exchange import binance
    except Exception:
        return
    if getattr(binance, "_e2e_parser_telemetry_installed", False):
        return
    binance._e2e_parser_telemetry_installed = True
    original_trade = binance.parse_agg_trade_ws
    original_depth = binance.parse_depth_diff_ws

    @wraps(original_trade)
    def trade(payload: dict[str, Any]) -> Any:
        started = time.perf_counter()
        event_ms = payload.get("T", payload.get("E"))
        source_age = (
            max(0.0, time.time() * 1000 - float(event_ms))
            if event_ms is not None
            else None
        )
        result = original_trade(payload)
        parse_ms = (time.perf_counter() - started) * 1000
        if source_age is not None and (source_age >= 1000 or parse_ms >= 10):
            from aitos.logging_setup import get_logger

            get_logger("aitos.forensics.e2e").warning(
                "trade source/parser attribution",
                extra={
                    "aitos_extra": {
                        "stage": "ws_to_parser",
                        "symbol": payload.get("s"),
                        "trade_id": payload.get("l"),
                        "exchange_event_ms": event_ms,
                        "source_age_ms": round(source_age, 3),
                        "parse_ms": round(parse_ms, 3),
                    }
                },
            )
        return result

    @wraps(original_depth)
    def depth(payload: dict[str, Any]) -> Any:
        started = time.perf_counter()
        event_ms = payload.get("E", payload.get("T"))
        source_age = (
            max(0.0, time.time() * 1000 - float(event_ms)) if event_ms else None
        )
        result = original_depth(payload)
        parse_ms = (time.perf_counter() - started) * 1000
        if source_age is not None and (source_age >= 1000 or parse_ms >= 10):
            from aitos.logging_setup import get_logger

            get_logger("aitos.forensics.e2e").warning(
                "depth source/parser attribution",
                extra={
                    "aitos_extra": {
                        "stage": "ws_to_parser",
                        "exchange_event_ms": event_ms,
                        "source_age_ms": round(source_age, 3),
                        "parse_ms": round(parse_ms, 3),
                    }
                },
            )
        return result

    binance.parse_agg_trade_ws = trade
    binance.parse_depth_diff_ws = depth


__all__ = ["install"]
