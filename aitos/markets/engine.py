"""Deterministic cross-market intelligence primitives."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, sqrt

from .contracts import MarketEvent
from .state import GlobalMarketState, MarketStateBuilder


@dataclass(frozen=True, slots=True)
class LeadLagResult:
    leader: str
    follower: str
    lag_steps: int
    correlation: float
    observations: int


class CrossMarketIntelligenceEngine:
    """Build market-independent features from canonical events.

    The engine deliberately stores compact feature samples rather than raw
    ticks. Raw events remain the responsibility of the market-data plane.
    """

    def __init__(self, max_samples: int = 2048) -> None:
        if max_samples < 32:
            raise ValueError("max_samples must be >= 32")
        self._series: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )
        self._timed_series: dict[str, deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )

    def ingest_price(self, event: MarketEvent, price: float | None = None) -> None:
        value = price if price is not None else float(event.payload.get("price", 0.0))
        if not isfinite(value) or value <= 0:
            return
        self._series[event.instrument.id].append(value)
        event_time = event.event_time
        if event_time.tzinfo is not None and event_time.utcoffset() is not None:
            timed = self._timed_series[event.instrument.id]
            if not timed or event_time > timed[-1][0]:
                timed.append((event_time, value))

    def returns(self, instrument_id: str) -> tuple[float, ...]:
        values = self._series[instrument_id]
        return tuple((b / a) - 1.0 for a, b in zip(values, list(values)[1:]) if a > 0)

    def correlation(self, left: str, right: str, lag: int = 0) -> float:
        a, b = self.returns(left), self.returns(right)
        if lag < 0:
            raise ValueError("lag must be >= 0")
        n = min(len(a) - lag, len(b))
        if n < 8:
            return 0.0
        x, y = a[lag : lag + n], b[:n]
        mx, my = sum(x) / n, sum(y) / n
        dx = [v - mx for v in x]
        dy = [v - my for v in y]
        denom = sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
        return 0.0 if denom == 0 else sum(u * v for u, v in zip(dx, dy)) / denom

    def discover_lead_lag(
        self, leader: str, follower: str, max_lag: int = 12
    ) -> LeadLagResult:
        best_lag, best_corr = 0, 0.0
        for lag in range(max_lag + 1):
            corr = self.correlation(leader, follower, lag)
            if abs(corr) > abs(best_corr):
                best_lag, best_corr = lag, corr
        return LeadLagResult(
            leader,
            follower,
            best_lag,
            best_corr,
            min(len(self.returns(leader)), len(self.returns(follower))),
        )

    def timestamped_returns(
        self, instrument_id: str, *, bucket_seconds: float = 1.0
    ) -> dict[int, float]:
        """Return adjacent-bucket returns keyed by UTC epoch bucket.

        Only buckets with observations in both the current and immediately
        preceding bucket are included. This avoids silently bridging data gaps
        or aligning asynchronous instruments by array index.
        """
        if not isfinite(bucket_seconds) or bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be finite and positive")
        bucket_prices: dict[int, float] = {}
        for event_time, price in self._timed_series[instrument_id]:
            bucket = int(event_time.timestamp() // bucket_seconds)
            bucket_prices[bucket] = price
        returns: dict[int, float] = {}
        for bucket, price in bucket_prices.items():
            previous = bucket_prices.get(bucket - 1)
            if previous is not None and previous > 0:
                returns[bucket] = price / previous - 1.0
        return returns

    def timestamp_aligned_correlation(
        self,
        leader: str,
        follower: str,
        *,
        lag_buckets: int = 0,
        bucket_seconds: float = 1.0,
        min_observations: int = 8,
    ) -> tuple[float, int]:
        """Correlate leader returns with follower returns at a time offset."""
        if isinstance(lag_buckets, bool) or lag_buckets < 0:
            raise ValueError("lag_buckets must be a non-negative integer")
        if int(lag_buckets) != lag_buckets:
            raise ValueError("lag_buckets must be a non-negative integer")
        if min_observations < 2:
            raise ValueError("min_observations must be >= 2")
        left = self.timestamped_returns(leader, bucket_seconds=bucket_seconds)
        right = self.timestamped_returns(follower, bucket_seconds=bucket_seconds)
        pairs = [
            (left[bucket], right[bucket + lag_buckets])
            for bucket in sorted(left)
            if bucket + lag_buckets in right
        ]
        n = len(pairs)
        if n < min_observations:
            return 0.0, n
        x = [pair[0] for pair in pairs]
        y = [pair[1] for pair in pairs]
        mean_x, mean_y = sum(x) / n, sum(y) / n
        centered_x = [value - mean_x for value in x]
        centered_y = [value - mean_y for value in y]
        denominator = sqrt(
            sum(value * value for value in centered_x)
            * sum(value * value for value in centered_y)
        )
        correlation = (
            0.0
            if denominator == 0
            else sum(a * b for a, b in zip(centered_x, centered_y)) / denominator
        )
        return correlation, n

    def discover_timestamp_aligned_lead_lag(
        self,
        leader: str,
        follower: str,
        *,
        max_lag_buckets: int = 12,
        bucket_seconds: float = 1.0,
        min_observations: int = 8,
    ) -> LeadLagResult:
        """Discover lead/lag using UTC time buckets rather than sample indices."""
        if isinstance(max_lag_buckets, bool) or max_lag_buckets < 0:
            raise ValueError("max_lag_buckets must be a non-negative integer")
        if int(max_lag_buckets) != max_lag_buckets:
            raise ValueError("max_lag_buckets must be a non-negative integer")
        best_lag, best_corr, best_observations = 0, 0.0, 0
        for lag in range(max_lag_buckets + 1):
            correlation, observations = self.timestamp_aligned_correlation(
                leader,
                follower,
                lag_buckets=lag,
                bucket_seconds=bucket_seconds,
                min_observations=min_observations,
            )
            if observations >= min_observations and abs(correlation) > abs(best_corr):
                best_lag, best_corr, best_observations = lag, correlation, observations
        return LeadLagResult(
            leader, follower, best_lag, best_corr, best_observations
        )

    def build_state(
        self,
        *,
        volatility: float,
        risk: float,
        liquidity: float,
        confidence: float = 0.5,
        features: dict[str, float] | None = None,
    ) -> GlobalMarketState:
        return MarketStateBuilder.build(
            volatility=volatility,
            risk=risk,
            liquidity=liquidity,
            confidence=confidence,
            features=features,
        )

    def snapshot_series(self) -> dict[str, tuple[float, ...]]:
        return {key: tuple(values) for key, values in self._series.items()}
