"""Bounded, on-demand historical market intelligence for promoted candidates.

Raw candles are fetched only after candidate promotion, cached in a bounded
process-local LRU, and never persisted by this service. A cache miss is
best-effort: historical context cannot block execution or relax risk controls.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Awaitable, Callable

from aitos.intelligence.historical_analogue import search_historical_analogues


KlineFetcher = Callable[..., Awaitable[list[Any]]]


@dataclass(frozen=True, slots=True)
class HistoricalIntelligenceConfig:
    cache_ttl_seconds: float = 900.0
    max_cache_entries: int = 24
    max_cached_candles: int = 12_000
    max_candles_per_request: int = 1_000
    max_request_weight_per_minute: int = 60
    max_concurrency: int = 2
    analogue_window: int = 20
    analogue_horizon: int = 12
    analogue_top_k: int = 8

    def __post_init__(self) -> None:
        if self.cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        if min(self.max_cache_entries, self.max_cached_candles,
               self.max_candles_per_request, self.max_request_weight_per_minute,
               self.max_concurrency, self.analogue_window,
               self.analogue_horizon, self.analogue_top_k) <= 0:
            raise ValueError("historical intelligence limits must be positive")
        if self.max_candles_per_request > 1500:
            raise ValueError("Binance kline request limit cannot exceed 1500")


def kline_request_weight(limit: int) -> int:
    """USD-M Futures kline request weight schedule."""
    if limit < 1 or limit > 1500:
        raise ValueError("kline limit must be between 1 and 1500")
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class OnDemandHistoricalIntelligence:
    """TTL/LRU cache, request coalescing, bounded concurrency and weight budget."""

    def __init__(
        self,
        fetcher: KlineFetcher,
        config: HistoricalIntelligenceConfig | None = None,
    ) -> None:
        self._fetcher = fetcher
        self.config = config or HistoricalIntelligenceConfig()
        self._cache: OrderedDict[tuple[str, str, int], tuple[float, tuple[Any, ...]]] = OrderedDict()
        self._inflight: dict[tuple[str, str, int], asyncio.Task[list[Any] | None]] = {}
        self._weight_events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._cached_candles = 0
        self.metrics = {
            "cache_hits": 0, "cache_misses": 0, "coalesced_requests": 0,
            "budget_rejections": 0, "fetch_errors": 0, "fetched_candles": 0,
        }

    def _weight_used(self, now: float) -> int:
        while self._weight_events and now - self._weight_events[0][0] >= 60.0:
            self._weight_events.popleft()
        return sum(weight for _, weight in self._weight_events)

    async def get_klines(
        self, symbol: str, timeframe: str, *, limit: int = 500
    ) -> list[Any] | None:
        limit = min(int(limit), self.config.max_candles_per_request)
        if limit < 1:
            return None
        key = (symbol.upper(), timeframe.lower(), limit)
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.config.cache_ttl_seconds:
                self._cache.move_to_end(key)
                self.metrics["cache_hits"] += 1
                return list(cached[1])
            if cached:
                self._cached_candles -= len(cached[1])
                del self._cache[key]
            task = self._inflight.get(key)
            if task is not None:
                self.metrics["coalesced_requests"] += 1
            else:
                weight = kline_request_weight(limit)
                if self._weight_used(now) + weight > self.config.max_request_weight_per_minute:
                    self.metrics["budget_rejections"] += 1
                    return None
                self._weight_events.append((now, weight))
                self.metrics["cache_misses"] += 1
                task = asyncio.create_task(self._load(key, limit))
                self._inflight[key] = task
        try:
            rows = await task
            return list(rows) if rows is not None else None
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():
                    self._inflight.pop(key, None)

    async def _load(
        self, key: tuple[str, str, int], limit: int
    ) -> list[Any] | None:
        try:
            async with self._semaphore:
                rows = await self._fetcher(key[0], key[1], limit=limit)
            ordered = sorted(rows, key=lambda item: getattr(item, "open_time", 0))
            if len(ordered) > limit:
                ordered = ordered[-limit:]
            if not ordered:
                return None
            frozen = tuple(ordered)
            async with self._lock:
                self._cache[key] = (time.monotonic(), frozen)
                self._cache.move_to_end(key)
                self._cached_candles += len(frozen)
                while (
                    len(self._cache) > self.config.max_cache_entries
                    or self._cached_candles > self.config.max_cached_candles
                ):
                    _, (_, evicted) = self._cache.popitem(last=False)
                    self._cached_candles -= len(evicted)
                self.metrics["fetched_candles"] += len(frozen)
            return list(frozen)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics["fetch_errors"] += 1
            return None

    def build_context(self, symbol: str, timeframe: str, klines: list[Any]) -> dict[str, Any]:
        """Produce deterministic historical evidence from completed prior candles."""
        closes = [float(k.close) for k in klines if float(k.close) > 0]
        if len(closes) < max(2 * self.config.analogue_window + self.config.analogue_horizon, 53):
            return {
                "available": False, "reason": "insufficient_history",
                "symbol": symbol.upper(), "timeframe": timeframe,
                "sample_count": len(closes),
            }
        returns = [(b - a) / a for a, b in zip(closes, closes[1:]) if a > 0]
        window = self.config.analogue_window
        analogues = search_historical_analogues(
            klines, window=window, search_back=min(500, len(klines)),
            top_k=self.config.analogue_top_k,
            forward_horizon=self.config.analogue_horizon,
        )
        outcome = analogues[0].outcome if analogues else None
        recent_return = (closes[-1] / closes[-21] - 1.0) if len(closes) > 21 else 0.0
        return {
            "available": bool(analogues),
            "reason": None if analogues else "no_valid_analogues",
            "symbol": symbol.upper(), "timeframe": timeframe,
            "sample_count": len(closes),
            "lookback_return_20": round(recent_return, 8),
            "realized_volatility": round(pstdev(returns[-20:]) if len(returns) >= 2 else 0.0, 8),
            "analogue_count": len(analogues),
            "analogue_indices": [item.start_index for item in analogues],
            "analogue_similarities": [round(item.similarity, 6) for item in analogues],
            "up_probability": outcome.up_probability if outcome else None,
            "down_probability": outcome.down_probability if outcome else None,
            "flat_probability": outcome.flat_probability if outcome else None,
            "median_forward_return": outcome.median_return if outcome else None,
            "best_forward_return": outcome.best_return if outcome else None,
            "worst_forward_return": outcome.worst_return if outcome else None,
            "evidence_only": True,
            "lookahead_policy": "matched windows and their forward outcomes end before current window",
        }

    async def analyze(
        self, symbol: str, timeframe: str, *, limit: int = 500
    ) -> tuple[list[Any] | None, dict[str, Any]]:
        rows = await self.get_klines(symbol, timeframe, limit=limit)
        if rows is None:
            return None, {
                "available": False, "reason": "historical_fetch_unavailable",
                "symbol": symbol.upper(), "timeframe": timeframe,
            }
        return rows, self.build_context(symbol, timeframe, rows)
