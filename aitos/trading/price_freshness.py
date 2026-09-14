"""Tracks the last time each symbol received a *live* price update.

``TradeLifecycle.update_price()`` -- including its authoritative hard-SL
check -- only ever runs when a ``market.trade``/``market.kline`` event
arrives via the live websocket pipeline for that symbol (see
``aitos.trading.market_context``'s bridge). This module is the shared
signal that lets ``aitos.trading.price_safety_net`` know when that live
path has gone quiet for a symbol and a REST-polled price should be used
instead, without every caller needing to agree on their own bookkeeping.
"""

from __future__ import annotations

import time


class PriceFreshnessTracker:
    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}

    def note_seen(self, symbol: str, when: float | None = None) -> None:
        self._last_seen[symbol.upper()] = when if when is not None else time.monotonic()

    def seconds_since_seen(
        self, symbol: str, now: float | None = None
    ) -> float | None:
        last = self._last_seen.get(symbol.upper())
        if last is None:
            return None
        return (now if now is not None else time.monotonic()) - last

    def is_stale(
        self, symbol: str, stale_after_seconds: float, now: float | None = None
    ) -> bool:
        elapsed = self.seconds_since_seen(symbol, now=now)
        return elapsed is None or elapsed >= stale_after_seconds


_GLOBAL_TRACKER = PriceFreshnessTracker()


def get_global_tracker() -> PriceFreshnessTracker:
    return _GLOBAL_TRACKER
