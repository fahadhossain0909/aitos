"""Public-API-only market-data telemetry for production diagnostics.

This module deliberately avoids private EventBus internals.  It observes
publish latency and lightweight counters without changing delivery semantics.
"""

from __future__ import annotations

import time
from dataclasses import replace
from functools import wraps
from typing import Any

_MARKET_PREFIXES = (
    "market.trade",
    "market.book",
    "market.orderbook",
    "market.liquidity",
    "market.live_state",
    "market.kline",
)


def _is_market_topic(topic: str) -> bool:
    return topic.startswith(_MARKET_PREFIXES)


def install(eventbus_cls: type[Any]) -> None:
    """Install safe telemetry using only public EventBus methods."""
    if getattr(eventbus_cls, "_safe_market_data_telemetry_installed", False):
        return
    eventbus_cls._safe_market_data_telemetry_installed = True

    original_init = eventbus_cls.__init__
    original_publish = eventbus_cls.publish
    original_health = eventbus_cls.health_check

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._market_publish_count = 0
        self._market_publish_errors = 0
        self._market_publish_total_ms = 0.0
        self._market_publish_max_ms = 0.0
        self._market_last_publish_ms = 0.0

    @wraps(original_publish)
    async def publish_wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        topic = getattr(event, "topic", "")
        if not _is_market_topic(topic):
            return await original_publish(self, event, *args, **kwargs)
        start = time.perf_counter()
        try:
            return await original_publish(self, event, *args, **kwargs)
        except Exception:
            self._market_publish_errors += 1
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._market_publish_count += 1
            self._market_publish_total_ms += elapsed_ms
            self._market_publish_max_ms = max(self._market_publish_max_ms, elapsed_ms)
            self._market_last_publish_ms = elapsed_ms

    async def health_wrapper(self: Any):
        status = await original_health(self)
        count = getattr(self, "_market_publish_count", 0)
        total = getattr(self, "_market_publish_total_ms", 0.0)
        details = {
            **status.details,
            "market_publish_latency": {
                "count": count,
                "total_ms": round(total, 3),
                "avg_ms": round(total / count, 3) if count else 0.0,
                "max_ms": round(getattr(self, "_market_publish_max_ms", 0.0), 3),
                "last_ms": round(getattr(self, "_market_last_publish_ms", 0.0), 3),
                "errors": getattr(self, "_market_publish_errors", 0),
            },
        }
        return replace(status, details=details)

    eventbus_cls.__init__ = init_wrapper
    eventbus_cls.publish = publish_wrapper
    eventbus_cls.health_check = health_wrapper


__all__ = ["install"]
