"""Venue-specific execution-cost quotes for capital rebalancing.

No global fee/slippage/funding defaults are used here. A venue adapter must
provide current taker fees, expected market-impact/slippage, funding over the
intended holding horizon, and liquidation distance for the actual leverage and
margin mode. Missing or stale quotes are rejected by the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite


@dataclass(frozen=True, slots=True)
class ExecutionCostQuote:
    venue: str
    symbol: str
    side: str
    round_trip_fee_bps: float
    expected_slippage_bps: float
    funding_cost_bps: float
    liquidation_distance_pct: float
    observed_at: str
    valid_for_seconds: float
    source: str

    def __post_init__(self) -> None:
        for name in (
            "round_trip_fee_bps",
            "expected_slippage_bps",
            "liquidation_distance_pct",
            "valid_for_seconds",
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not isfinite(float(self.funding_cost_bps)):
            raise ValueError("funding_cost_bps must be finite")
        if not all((self.venue, self.symbol, self.side, self.observed_at, self.source)):
            raise ValueError("venue quote identity and source fields are required")

    def as_candidate_costs(self) -> dict[str, float]:
        return {
            "fee_bps": self.round_trip_fee_bps,
            "slippage_bps": self.expected_slippage_bps,
            "funding_bps": self.funding_cost_bps,
            "liquidation_distance_pct": self.liquidation_distance_pct,
        }


VenueQuoteLoader = Callable[..., ExecutionCostQuote]


class VenueExecutionCostRegistry:
    """Resolve only explicitly registered venue adapters; never guess costs."""

    def __init__(self, *, max_quote_age_seconds: float = 30.0) -> None:
        if not isfinite(max_quote_age_seconds) or max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be finite and positive")
        self.max_quote_age_seconds = max_quote_age_seconds
        self._loaders: dict[str, VenueQuoteLoader] = {}

    def register(self, venue: str, loader: VenueQuoteLoader) -> None:
        key = venue.strip().lower()
        if not key or not callable(loader):
            raise ValueError("venue and callable quote loader are required")
        self._loaders[key] = loader

    async def quote(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        notional_usd: float,
        leverage: float,
        holding_horizon_seconds: float,
    ) -> ExecutionCostQuote:
        loader = self._loaders.get(venue.strip().lower())
        if loader is None:
            raise LookupError(f"no execution-cost adapter registered for {venue}")
        result = loader(
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            leverage=leverage,
            holding_horizon_seconds=holding_horizon_seconds,
        )
        quote = await result if hasattr(result, "__await__") else result
        if not isinstance(quote, ExecutionCostQuote):
            raise TypeError("venue adapter must return ExecutionCostQuote")
        if quote.venue.strip().lower() != venue.strip().lower():
            raise ValueError("execution-cost quote venue mismatch")
        if quote.symbol.strip().upper() != symbol.strip().upper():
            raise ValueError("execution-cost quote symbol mismatch")
        if quote.side.strip().upper() != side.strip().upper():
            raise ValueError("execution-cost quote side mismatch")
        if quote.valid_for_seconds > self.max_quote_age_seconds:
            raise ValueError("execution-cost quote validity exceeds configured maximum")
        try:
            observed_at = datetime.fromisoformat(
                quote.observed_at.replace("Z", "+00:00")
            )
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        except (TypeError, ValueError) as exc:
            raise ValueError("execution-cost quote has invalid observed_at") from exc
        if age < -1.0 or age > min(quote.valid_for_seconds, self.max_quote_age_seconds):
            raise ValueError("execution-cost quote is stale or from the future")
        return quote
