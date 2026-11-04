"""Candle-close-aware cache for higher-timeframe structural context."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Hashable


def timeframe_delta(timeframe: str) -> timedelta:
    unit = timeframe[-1]
    amount = int(timeframe[:-1])
    if amount < 1:
        raise ValueError("timeframe amount must be positive")
    seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit == "M":
        # Calendar months vary; a conservative 31-day refresh avoids
        # premature refreshes for monthly bars.
        return timedelta(days=31 * amount)
    if unit not in seconds:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return timedelta(seconds=seconds[unit] * amount)


@dataclass(frozen=True)
class _Entry:
    candles: tuple
    latest_close: datetime
    refresh_after: datetime


class HigherTimeframeCandleCache:
    """Single-flight cache refreshed only after the next HTF candle closes.

    Only closed candles are retained. If the exchange returns an incomplete or
    unchanged window after a refresh boundary, the last known-good snapshot is
    preserved and returned; callers can fail closed based on its age.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[Hashable, _Entry] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}

    async def get(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        fetch: Callable[[], Awaitable[list]],
    ) -> tuple[list, dict]:
        key = (symbol.upper(), timeframe, int(limit))
        now = self._clock().astimezone(timezone.utc)
        entry = self._entries.get(key)
        if entry is not None and now < entry.refresh_after:
            return list(entry.candles), {
                "cache": "hit",
                "latest_closed_at": entry.latest_close.isoformat(),
            }

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            now = self._clock().astimezone(timezone.utc)
            if entry is not None and now < entry.refresh_after:
                return list(entry.candles), {
                    "cache": "hit_after_wait",
                    "latest_closed_at": entry.latest_close.isoformat(),
                }

            fetched = await fetch()
            closed = [
                candle for candle in fetched if getattr(candle, "is_closed", True)
            ]
            closed.sort(key=lambda candle: candle.close_time)
            if not closed:
                if entry is not None:
                    return list(entry.candles), {
                        "cache": "stale_fallback",
                        "stale": True,
                        "latest_closed_at": entry.latest_close.isoformat(),
                    }
                return [], {"cache": "miss_empty", "latest_closed_at": None}

            latest_close = closed[-1].close_time
            if latest_close.tzinfo is None:
                latest_close = latest_close.replace(tzinfo=timezone.utc)
            else:
                latest_close = latest_close.astimezone(timezone.utc)

            if entry is not None and latest_close <= entry.latest_close:
                # Do not spin on a lagging REST replica; retry after a short
                # backoff while continuing to serve the last known-good bars.
                self._entries[key] = _Entry(
                    entry.candles,
                    entry.latest_close,
                    now + timedelta(seconds=5),
                )
                return list(entry.candles), {
                    "cache": "unchanged_fallback",
                    "stale": True,
                    "latest_closed_at": entry.latest_close.isoformat(),
                }

            next_close = latest_close + timeframe_delta(timeframe)
            self._entries[key] = _Entry(
                tuple(closed[-limit:]), latest_close, next_close
            )
            return list(self._entries[key].candles), {
                "cache": "refreshed",
                "latest_closed_at": latest_close.isoformat(),
                "refresh_after": next_close.isoformat(),
            }

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._entries.clear()
            return
        normalized = symbol.upper()
        for key in list(self._entries):
            if key[0] == normalized:
                self._entries.pop(key, None)
