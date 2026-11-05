"""Candle-close-aware cache for higher-timeframe structural context."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def timeframe_delta(timeframe: str) -> timedelta:
    if not timeframe or len(timeframe) < 2:
        raise ValueError("timeframe must contain a positive amount and unit")
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

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        max_requests: int = 60,
        window_seconds: float = 60.0,
        max_concurrent_fetches: int = 4,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0 or max_concurrent_fetches < 1:
            raise ValueError("HTF request budgets and concurrency must be positive")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[Hashable, _Entry] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._budget_lock = asyncio.Lock()
        self._request_times: list[datetime] = []
        self._max_requests = int(max_requests)
        self._window = timedelta(seconds=window_seconds)
        self._fetch_semaphore = asyncio.Semaphore(max_concurrent_fetches)

    async def _reserve_request(self) -> bool:
        """Reserve one exchange request from the shared sliding-window budget."""
        async with self._budget_lock:
            now = self._clock().astimezone(timezone.utc)
            cutoff = now - self._window
            self._request_times = [
                stamp for stamp in self._request_times if stamp > cutoff
            ]
            if len(self._request_times) >= self._max_requests:
                return False
            self._request_times.append(now)
            return True

    def _budget_fallback(self, entry: _Entry | None) -> tuple[list, dict]:
        if entry is None:
            return [], {"cache": "budget_exhausted", "stale": True}
        return list(entry.candles), {
            "cache": "budget_exhausted_fallback",
            "stale": True,
            "latest_closed_at": entry.latest_close.isoformat(),
        }

    async def get(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        fetch: Callable[[], Awaitable[list]],
    ) -> tuple[list, dict]:
        if limit < 1:
            raise ValueError("limit must be positive")
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

            if not await self._reserve_request():
                return self._budget_fallback(entry)
            async with self._fetch_semaphore:
                fetched = await fetch()
            closed = [
                candle for candle in fetched if getattr(candle, "is_closed", True)
            ]
            closed.sort(key=lambda candle: candle.close_time)
            if not closed:
                if entry is not None:
                    # A forming-only response is evidence that the endpoint
                    # has not published the newly closed bar yet, not an empty
                    # cache. Keep serving the last good snapshot with backoff.
                    self._entries[key] = _Entry(
                        entry.candles,
                        entry.latest_close,
                        now + timedelta(seconds=5),
                    )
                    status = "unchanged_fallback" if fetched else "stale_fallback"
                    return list(entry.candles), {
                        "cache": status,
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
