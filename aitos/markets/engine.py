"""Deterministic cross-market intelligence primitives."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import sqrt

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

    def ingest_price(self, event: MarketEvent, price: float | None = None) -> None:
        value = price if price is not None else float(event.payload.get("price", 0.0))
        if value > 0:
            self._series[event.instrument.id].append(value)

    def returns(self, instrument_id: str) -> tuple[float, ...]:
        values = self._series[instrument_id]
        return tuple(
            (b / a) - 1.0 for a, b in zip(values, list(values)[1:]) if a > 0
        )

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
