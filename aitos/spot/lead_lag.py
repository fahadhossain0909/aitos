"""Causal Futures↔Spot lead-lag diagnostics on synchronized market returns.

The analyzer is observational only: it never emits an order, direction override,
or strategy score. It requires timestamped prices from both markets, computes
both lead directions, and exposes sample counts and Fisher confidence intervals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.market_data.bus import MarketDataBus
from aitos.market_data.contracts import MarketEvent, MarketEventType

FUTURES_MARKET_TYPES = frozenset({"usd_m_futures", "coin_m_futures", "perpetual"})
SPOT_MARKET_TYPE = "spot"


@dataclass(frozen=True, slots=True)
class LeadLagEstimate:
    symbol: str
    status: str
    leader: str | None
    lag_seconds: int | None
    correlation: float | None
    confidence_low: float | None
    confidence_high: float | None
    observations: int
    as_of: datetime


@dataclass(slots=True)
class _PairState:
    futures: dict[int, float]
    spot: dict[int, float]


class FuturesSpotLeadLagAnalyzer:
    """Estimate whether Futures returns lead Spot returns (or vice versa)."""

    def __init__(
        self,
        *,
        bucket_seconds: int = 1,
        max_lag_seconds: int = 5,
        window_seconds: int = 900,
        min_observations: int = 60,
        min_abs_correlation: float = 0.25,
        min_lead_advantage: float = 0.05,
        futures_market_types: frozenset[str] = FUTURES_MARKET_TYPES,
        symbol_aliases: dict[str, str] | None = None,
        max_symbols: int = 100,
    ) -> None:
        if bucket_seconds <= 0 or max_lag_seconds < 0:
            raise ValueError("invalid lead-lag time resolution")
        if window_seconds <= max_lag_seconds + 2 * bucket_seconds:
            raise ValueError("lead-lag window is too short for configured lag")
        if min_observations < 10:
            raise ValueError("min_observations must be at least 10")
        if not 0 <= min_abs_correlation < 1:
            raise ValueError("min_abs_correlation must be in [0, 1)")
        if not 0 <= min_lead_advantage < 1:
            raise ValueError("min_lead_advantage must be in [0, 1)")
        if not 1 <= max_symbols <= 500:
            raise ValueError("max_symbols must be between 1 and 500")
        self.max_symbols = max_symbols
        self.bucket_seconds = bucket_seconds
        self.max_lag_buckets = math.ceil(max_lag_seconds / bucket_seconds)
        self.window_buckets = math.ceil(window_seconds / bucket_seconds)
        self.min_observations = min_observations
        self.min_abs_correlation = min_abs_correlation
        self.min_lead_advantage = min_lead_advantage
        self.futures_market_types = frozenset(
            str(value).lower() for value in futures_market_types
        )
        # Aliases map incoming Futures symbols to canonical Spot symbols.
        self.symbol_aliases = {
            str(key).upper(): str(value).upper()
            for key, value in (symbol_aliases or {}).items()
        }
        self._allowed_symbols: frozenset[str] | None = None
        self._pairs: dict[str, _PairState] = {}
        self._estimates: dict[str, LeadLagEstimate] = {}
        self._subscriptions = []
        self._started = False
        self._accepted = 0
        self._rejected = 0

    def set_symbols(self, symbols: list[str] | tuple[str, ...]) -> None:
        """Restrict analysis to the selected Spot universe after alias mapping."""
        normalized = frozenset(str(symbol).upper() for symbol in symbols if symbol)
        if not normalized or len(normalized) > self.max_symbols:
            raise ValueError("lead-lag symbol universe is empty or exceeds capacity")
        self._allowed_symbols = normalized
        self._pairs = {
            symbol: pair for symbol, pair in self._pairs.items()
            if symbol in normalized
        }
        self._estimates = {
            symbol: estimate for symbol, estimate in self._estimates.items()
            if symbol in normalized
        }

    def _canonical_symbol(self, event: MarketEvent) -> str:
        symbol = event.symbol.upper()
        return self.symbol_aliases.get(symbol, symbol)

    @staticmethod
    def _price(event: MarketEvent) -> float | None:
        payload = event.payload
        try:
            if (
                payload.get("bid_price") is not None
                and payload.get("ask_price") is not None
            ):
                bid = float(payload["bid_price"])
                ask = float(payload["ask_price"])
                value = (bid + ask) / 2.0
            else:
                value = float(
                    payload.get("last_price")
                    or payload.get("lastPrice")
                    or payload.get("close")
                    or payload.get("c")
                    or payload.get("mark_price")
                    or payload.get("price")
                )
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def ingest(self, event: MarketEvent) -> LeadLagEstimate | None:
        if event.event_type != MarketEventType.TICKER:
            self._rejected += 1
            return None
        market_type = str(event.market_type or event.market).lower()
        if market_type == SPOT_MARKET_TYPE:
            side = "spot"
        elif market_type in self.futures_market_types:
            side = "futures"
        else:
            self._rejected += 1
            return None
        price = self._price(event)
        if price is None or event.event_time.tzinfo is None:
            self._rejected += 1
            return None
        symbol = self._canonical_symbol(event)
        if not symbol or (
            self._allowed_symbols is not None and symbol not in self._allowed_symbols
        ):
            self._rejected += 1
            return None
        bucket = int(event.event_time.timestamp()) // self.bucket_seconds
        if symbol not in self._pairs and len(self._pairs) >= self.max_symbols:
            self._rejected += 1
            return None
        pair = self._pairs.setdefault(symbol, _PairState({}, {}))
        series = pair.futures if side == "futures" else pair.spot
        series[bucket] = price
        newest = max(pair.futures.keys() | pair.spot.keys())
        cutoff = newest - self.window_buckets - self.max_lag_buckets - 2
        for values in (pair.futures, pair.spot):
            for old_bucket in tuple(values):
                if old_bucket < cutoff:
                    del values[old_bucket]
        self._accepted += 1
        estimate = self._estimate(symbol, pair, event.event_time)
        if estimate is not None:
            self._estimates[symbol] = estimate
        return estimate

    def _estimate(
        self, symbol: str, pair: _PairState, as_of: datetime
    ) -> LeadLagEstimate | None:
        futures_returns = self._returns(pair.futures)
        spot_returns = self._returns(pair.spot)
        if (
            len(futures_returns) < self.min_observations
            or len(spot_returns) < self.min_observations
        ):
            return LeadLagEstimate(
                symbol,
                "insufficient_data",
                None,
                None,
                None,
                None,
                None,
                min(len(futures_returns), len(spot_returns)),
                as_of,
            )
        candidates: list[tuple[float, str, int, int]] = []
        for lag in range(0, self.max_lag_buckets + 1):
            corr, count = self._lag_correlation(
                futures_returns, spot_returns, lag
            )
            if corr is not None and count >= self.min_observations:
                candidates.append((abs(corr), "synchronous" if lag == 0 else "futures", lag, count))
            if lag:
                corr, count = self._lag_correlation(
                    spot_returns, futures_returns, lag
                )
                if corr is not None and count >= self.min_observations:
                    candidates.append((abs(corr), "spot", lag, count))
        if not candidates:
            return LeadLagEstimate(
                symbol, "insufficient_aligned_returns", None, None, None,
                None, None, 0, as_of
            )
        candidates.sort(key=lambda item: item[0], reverse=True)
        strength, leader, lag, count = candidates[0]
        signed_corr, _ = self._lag_correlation(
            futures_returns, spot_returns, lag
        ) if leader == "futures" else self._lag_correlation(
            spot_returns, futures_returns, lag
        )
        assert signed_corr is not None
        low, high = self._fisher_interval(signed_corr, count)
        runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
        if strength < self.min_abs_correlation:
            status, leader_value, lag_value = "no_material_lead", None, None
        elif low <= 0 <= high:
            status, leader_value, lag_value = "low_confidence", None, None
        elif leader == "synchronous":
            status, leader_value, lag_value = "synchronous", None, None
        elif strength - runner_up < self.min_lead_advantage:
            status, leader_value, lag_value = "ambiguous_lead", None, None
        else:
            status, leader_value, lag_value = (
                f"{leader}_leads",
                leader,
                (lag * self.bucket_seconds),
            )
        return LeadLagEstimate(
            symbol,
            status,
            leader_value,
            lag_value,
            signed_corr,
            low,
            high,
            count,
            as_of,
        )

    def _returns(self, prices: dict[int, float]) -> dict[int, float]:
        ordered = sorted(prices.items())
        result: dict[int, float] = {}
        for (previous_bucket, previous), (bucket, current) in zip(ordered, ordered[1:]):
            if bucket - previous_bucket != 1:
                continue
            result[bucket] = math.log(current / previous)
        return result

    @staticmethod
    def _lag_correlation(
        leader: dict[int, float], follower: dict[int, float], lag: int
    ) -> tuple[float | None, int]:
        paired = [
            (leader[bucket], follower[bucket + lag])
            for bucket in leader
            if bucket + lag in follower
        ]
        if len(paired) < 3:
            return None, len(paired)
        xs, ys = zip(*paired)
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        covariance = sum((x - mean_x) * (y - mean_y) for x, y in paired)
        variance_x = sum((x - mean_x) ** 2 for x in xs)
        variance_y = sum((y - mean_y) ** 2 for y in ys)
        denominator = math.sqrt(variance_x * variance_y)
        if denominator <= 0:
            return None, len(paired)
        return max(-1.0, min(1.0, covariance / denominator)), len(paired)

    @staticmethod
    def _fisher_interval(correlation: float, count: int) -> tuple[float, float]:
        if count <= 3:
            return -1.0, 1.0
        clipped = max(-0.999999, min(0.999999, correlation))
        z = math.atanh(clipped)
        margin = 1.96 / math.sqrt(count - 3)
        return math.tanh(z - margin), math.tanh(z + margin)

    async def start(self, market_bus: MarketDataBus) -> None:
        if self._started:
            return
        try:
            self._subscriptions.append(
                await market_bus.subscribe(
                    MarketEventType.TICKER,
                    self._on_futures,
                    group="futures-spot-leadlag-futures-v1",
                    live_only=True,
                    market_type="usd_m_futures",
                )
            )
            self._subscriptions.append(
                await market_bus.subscribe(
                    MarketEventType.TICKER,
                    self._on_spot,
                    group="futures-spot-leadlag-spot-v1",
                    live_only=True,
                    market_type="spot",
                )
            )
        except Exception:
            await self.stop()
            raise
        self._started = True

    async def _on_futures(self, event: MarketEvent) -> None:
        if str(event.market_type).lower() in self.futures_market_types:
            self.ingest(event)

    async def _on_spot(self, event: MarketEvent) -> None:
        if str(event.market_type).lower() == SPOT_MARKET_TYPE:
            self.ingest(event)

    async def stop(self) -> None:
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            subscription.cancel()
        self._started = False

    def estimate(self, symbol: str) -> LeadLagEstimate | None:
        """Return the latest causal cross-market estimate for one Spot symbol."""
        return self._estimates.get(symbol.upper())

    def snapshot(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "accepted_events": self._accepted,
            "rejected_events": self._rejected,
            "symbols": {
                symbol: {
                    "status": estimate.status,
                    "leader": estimate.leader,
                    "lag_seconds": estimate.lag_seconds,
                    "correlation": estimate.correlation,
                    "confidence_interval_95": [
                        estimate.confidence_low,
                        estimate.confidence_high,
                    ],
                    "observations": estimate.observations,
                    "as_of": estimate.as_of.isoformat(),
                }
                for symbol, estimate in self._estimates.items()
            },
        }
