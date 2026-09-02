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
    if getattr(eventbus_cls, "_market_data_attribution_installed", False):
        return
    eventbus_cls._market_data_attribution_installed = True
    original_init = eventbus_cls.__init__
    original_process = eventbus_cls._process_message
    original_health = eventbus_cls.health_check

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._market_handler_count = defaultdict(int)
        self._market_handler_total_ms = defaultdict(float)
        self._market_handler_max_ms = defaultdict(float)

    @wraps(original_process)
    async def process_wrapper(
        self: Any,
        stream_key: Any,
        entry_id: Any,
        fields: dict[str, Any],
        group: str,
        handler: Any,
    ) -> None:
        try:
            from aitos.core.contracts import Event

            topic = Event.from_wire(fields).topic
        except Exception:
            topic = ""
        if not _is_market_topic(topic):
            await original_process(self, stream_key, entry_id, fields, group, handler)
            return
        start = time.perf_counter()
        try:
            await original_process(self, stream_key, entry_id, fields, group, handler)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._market_handler_count[topic] += 1
            self._market_handler_total_ms[topic] += elapsed_ms
            self._market_handler_max_ms[topic] = max(
                self._market_handler_max_ms[topic], elapsed_ms
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
    eventbus_cls._process_message = process_wrapper
    eventbus_cls.health_check = health_wrapper


__all__ = ["install_eventbus_attribution"]
