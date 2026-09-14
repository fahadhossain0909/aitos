"""Track last-seen live prices per symbol for staleness detection."""

from __future__ import annotations

import time


class PriceFreshnessTracker:
    """In-memory last-seen timestamps for live market prices."""

    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}

    def note_seen(self, symbol: str, when: float | None = None) -> None:
        self._last_seen[str(symbol).upper()] = float(
            when if when is not None else time.time()
        )

    def last_seen(self, symbol: str) -> float | None:
        return self._last_seen.get(str(symbol).upper())

    def age_seconds(self, symbol: str, now: float | None = None) -> float | None:
        ts = self.last_seen(symbol)
        if ts is None:
            return None
        return float(now if now is not None else time.time()) - ts

    def is_stale(
        self, symbol: str, stale_after_seconds: float, now: float | None = None
    ) -> bool:
        age = self.age_seconds(symbol, now=now)
        if age is None:
            return True
        return age >= float(stale_after_seconds)


_GLOBAL: PriceFreshnessTracker | None = None


def get_global_tracker() -> PriceFreshnessTracker:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = PriceFreshnessTracker()
    return _GLOBAL
