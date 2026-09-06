"""Low-overhead EventBus attribution for production market-data forensics.

Observational only: ACK, retry, ordering, and delivery semantics are unchanged.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import replace
from functools import wraps
from typing import Any

_MARKET_PREFIXES = (
    "market.trade.",
    "market.orderbook.",
    "market.orderflow.",
    "market.liquidity.",
    "market.live_state.",
    "market.kline.",
)


def _is_market_topic(topic: str) -> bool:
    return topic.startswith(_MARKET_PREFIXES)


def install_eventbus_attribution(eventbus_cls: type[Any]) -> None:
    """Install handler-level latency attribution without touching delivery internals.

    The EventBus implementation is intentionally treated as a black box here.
    Wrapping ``subscribe`` avoids coupling diagnostics to private consumer-loop
    methods, so changes to ACK/retry internals cannot break module import.
    """
    if getattr(eventbus_cls, "_market_data_attribution_installed", False):
        return
    eventbus_cls._market_data_attribution_installed = True
    original_init = eventbus_cls.__init__
    original_subscribe = eventbus_cls.subscribe
    original_health = eventbus_cls.health_check

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._market_handler_count = defaultdict(int)
        self._market_handler_total_ms = defaultdict(float)
        self._market_handler_max_ms = defaultdict(float)

    @wraps(original_subscribe)
    async def subscribe_wrapper(
        self: Any,
        topic: str,
        handler: Any,
        group: str = "default",
        start_id: str = "0",
    ) -> Any:
        @wraps(handler)
        async def observed_handler(event: Any) -> Any:
            event_topic = getattr(event, "topic", "")
            if not _is_market_topic(event_topic):
                return await handler(event)
            start = time.perf_counter()
            try:
                return await handler(event)
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                self._market_handler_count[event_topic] += 1
                self._market_handler_total_ms[event_topic] += elapsed_ms
                self._market_handler_max_ms[event_topic] = max(
                    self._market_handler_max_ms[event_topic], elapsed_ms
                )

        return await original_subscribe(
            self,
            topic,
            observed_handler,
            group=group,
            start_id=start_id,
        )

    async def health_wrapper(self: Any):
        status = await original_health(self)
        counts = getattr(self, "_market_handler_count", {})
        total = getattr(self, "_market_handler_total_ms", {})
        maximum = getattr(self, "_market_handler_max_ms", {})
        details = {
            **status.details,
            "market_handler_latency": {
                topic: {
                    "count": counts.get(topic, 0),
                    "total_ms": round(total.get(topic, 0.0), 3),
                    "max_ms": round(maximum.get(topic, 0.0), 3),
                    "avg_ms": (
                        round(total.get(topic, 0.0) / count, 3)
                        if (count := counts.get(topic, 0))
                        else 0.0
                    ),
                }
                for topic in sorted(counts)
            },
        }
        return replace(status, details=details)

    eventbus_cls.__init__ = init_wrapper
    eventbus_cls.subscribe = subscribe_wrapper
    eventbus_cls.health_check = health_wrapper


__all__ = ["install_eventbus_attribution"]
