"""Tracks the last time each symbol received a *live* price update.

``TradeLifecycle.update_price()`` -- including its authoritative hard-SL
check -- only ever runs when a ``market.trade``/``market.kline`` event
arrives via the live websocket pipeline for that symbol (see
``aitos.trading.market_context.handle_position_market_event``). If that
pipeline stalls, open positions stop being checked entirely.

This module is the shared clock for the position-price safety net: the
live path stamps symbols as seen, and the safety net treats any open
position whose stamp is older than a threshold as stale and fetches a
REST price so hard-SL can still fire.
"""

from __future__ import annotations

import time
from typing import Dict


class PriceFreshnessTracker:
    """In-memory last-seen timestamps for live price updates per symbol."""

    def __init__(self) -> None:
        self._last_seen: Dict[str, float] = {}

    def note_seen(self, symbol: str, when: float | None = None) -> None:
        key = str(symbol or "").upper()
        if not key:
            return
        self._last_seen[key] = float(when if when is not None else time.monotonic())

    def last_seen(self, symbol: str) -> float | None:
        return self._last_seen.get(str(symbol or "").upper())

    def is_stale(self, symbol: str, stale_after_seconds: float) -> bool:
        ts = self.last_seen(symbol)
        if ts is None:
            return True
        return (time.monotonic() - ts) > float(stale_after_seconds)

    def clear(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._last_seen.clear()
            return
        self._last_seen.pop(str(symbol).upper(), None)


_GLOBAL = PriceFreshnessTracker()


def get_global_tracker() -> PriceFreshnessTracker:
    return _GLOBAL
