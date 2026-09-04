"""EventBus market-data telemetry that uses only public EventBus methods."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import replace
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


def _stats_add(stats: dict[str, dict[str, float]], topic: str, ms: float) -> None:
    row = stats.setdefault(topic, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
    row["count"] += 1
    row["total_ms"] += ms
    row["max_ms"] = max(row["max_ms"], ms)


def _format(stats: dict[str, dict[str, float]]) -> dict[str, dict[str, float | int]]:
    return {
        topic: {
            "count": int(row["count"]),
            "total_ms": round(row["total_ms"], 3),
            "avg_ms": round(row["total_ms"] / row["count"], 3)
            if row["count"]
            else 0.0,
            "max_ms": round(row["max_ms"], 3),
        }
        for topic, row in sorted(stats.items())
    }


def install(eventbus_cls: type[Any]) -> None:
    """Add non-invasive telemetry without depending on private EventBus methods."""
    if getattr(eventbus_cls, "_safe_market_data_telemetry_installed", False):
        return
    eventbus_cls._safe_market_data_telemetry_installed = True
    original_init = eventbus_cls.__init__
    original_publish = eventbus_cls.publish
    original_subscribe = eventbus_cls.subscribe
    original_health = eventbus_cls.health_check
    original_shutdown = eventbus_cls.shutdown

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._safe_market_publish_stats: dict[str, dict[str, float]] = {}
        self._safe_market_handler_stats: dict[str, dict[str, float]] = {}
        self._safe_market_recent: deque[dict[str, Any]] = deque(maxlen=100)
        self._safe_market_loop_lag_ms = 0.0
        self._safe_market_loop_lag_max_ms = 0.0
        self._safe_market_loop_samples = 0
        try:
            self._safe_market_watchdog = asyncio.create_task(_watchdog(self))
        except RuntimeError:
            self._safe_market_watchdog = None

    @wraps(original_publish)
    async def publish(self: Any, event: Any, *args: Any, **kwargs: Any) -> None:
        topic = getattr(event, "topic", "")
        if not _market(topic):
            await original_publish(self, event, *args, **kwargs)
            return
        started = time.perf_counter()
        try:
            await original_publish(self, event, *args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            _stats_add(self._safe_market_publish_stats, topic, elapsed)

    @wraps(original_subscribe)
    async def subscribe(
        self: Any,
        topic: str,
        handler: Any,
        group: str = "default",
        start_id: str = "0",
    ) -> Any:
        @wraps(handler)
        async def observed(event: Any) -> Any:
            event_topic = getattr(event, "topic", "")
            if not _market(event_topic):
                return await handler(event)
            started = time.perf_counter()
            try:
                return await handler(event)
            finally:
                elapsed = (time.perf_counter() - started) * 1000.0
                _stats_add(self._safe_market_handler_stats, event_topic, elapsed)
                self._safe_market_recent.append(
                    {
                        "topic": event_topic,
                        "stage": "handler",
                        "latency_ms": round(elapsed, 3),
                        "ts": time.time(),
                    }
                )

        return await original_subscribe(
            self, topic, observed, group=group, start_id=start_id
        )

    @wraps(original_health)
    async def health(self: Any):
        status = await original_health(self)
        details = dict(status.details)
        details["market_data_e2e"] = {
            "publish_latency": _format(self._safe_market_publish_stats),
            "handler_latency": _format(self._safe_market_handler_stats),
            "event_loop": {
                "samples": self._safe_market_loop_samples,
                "last_lag_ms": round(self._safe_market_loop_lag_ms, 3),
                "max_lag_ms": round(self._safe_market_loop_lag_max_ms, 3),
            },
            "recent_traces": list(self._safe_market_recent),
        }
        return replace(status, details=details)

    @wraps(original_shutdown)
    async def shutdown(self: Any, *args: Any, **kwargs: Any) -> None:
        task = getattr(self, "_safe_market_watchdog", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await original_shutdown(self, *args, **kwargs)

    eventbus_cls.__init__ = init
    eventbus_cls.publish = publish
    eventbus_cls.subscribe = subscribe
    eventbus_cls.health_check = health
    eventbus_cls.shutdown = shutdown


async def _watchdog(eventbus: Any) -> None:
    loop = asyncio.get_running_loop()
    interval = 1.0
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        lag = max(0.0, (loop.time() - expected) * 1000.0)
        eventbus._safe_market_loop_samples += 1
        eventbus._safe_market_loop_lag_ms = lag
        eventbus._safe_market_loop_lag_max_ms = max(
            eventbus._safe_market_loop_lag_max_ms, lag
        )


__all__ = ["install"]
