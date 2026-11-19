"""Conservative Spot opportunity ranking; ranking is not an order signal."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SpotCandidate:
    symbol: str
    quote_volume: Decimal
    relative_volume: Decimal
    spread_bps: Decimal
    volatility_bps: Decimal
    momentum_bps: Decimal
    order_book_imbalance: Decimal
    trading_enabled: bool = True
    data_fresh: bool = True
    futures_spot_lead_lag_status: str = "unavailable"
    futures_spot_leader: str | None = None
    futures_spot_lag_seconds: int | None = None
    futures_spot_correlation: Decimal | None = None
    futures_spot_confidence_low: Decimal | None = None
    futures_spot_confidence_high: Decimal | None = None
    futures_spot_observations: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        fields = (
            "quote_volume",
            "relative_volume",
            "spread_bps",
            "volatility_bps",
            "momentum_bps",
            "order_book_imbalance",
        )
        for name in fields:
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                value = Decimal(str(value))
                object.__setattr__(self, name, value)
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.quote_volume < 0 or self.relative_volume < 0:
            raise ValueError("volume values cannot be negative")
        if self.spread_bps < 0 or self.volatility_bps < 0:
            raise ValueError("spread and volatility cannot be negative")
        if abs(self.order_book_imbalance) > 1:
            raise ValueError("order book imbalance must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class RankedSpotCandidate:
    candidate: SpotCandidate
    score: Decimal
    reasons: tuple[str, ...]


class SpotOpportunityScanner:
    """Deterministic, explainable pre-filter. A downstream strategy must approve."""

    def __init__(
        self,
        *,
        min_quote_volume: Decimal = Decimal("100000"),
        max_spread_bps: Decimal = Decimal("20"),
        max_volatility_bps: Decimal = Decimal("1500"),
    ) -> None:
        if min_quote_volume < 0 or max_spread_bps <= 0 or max_volatility_bps <= 0:
            raise ValueError("invalid scanner thresholds")
        self.min_quote_volume = min_quote_volume
        self.max_spread_bps = max_spread_bps
        self.max_volatility_bps = max_volatility_bps

    def rank(self, candidates: list[SpotCandidate]) -> list[RankedSpotCandidate]:
        ranked = []
        for item in candidates:
            if not item.trading_enabled or not item.data_fresh:
                continue
            if item.quote_volume < self.min_quote_volume:
                continue
            if item.spread_bps < 0 or item.spread_bps > self.max_spread_bps:
                continue
            if item.volatility_bps < 0 or item.volatility_bps > self.max_volatility_bps:
                continue
            if item.relative_volume < 0 or abs(item.order_book_imbalance) > 1:
                continue
            score = (
                min(item.relative_volume, Decimal("5")) * Decimal("0.25")
                + min(max(item.momentum_bps, Decimal("0")), Decimal("500"))
                / Decimal("500")
                * Decimal("0.25")
                + max(Decimal("0"), item.order_book_imbalance) * Decimal("0.25")
                + max(
                    Decimal("0"), Decimal("1") - item.spread_bps / self.max_spread_bps
                )
                * Decimal("0.25")
            )
            reasons = (
                "liquid_quote_volume",
                "spread_within_limit",
                "fresh_market_data",
                "relative_volume_and_flow",
            )
            ranked.append(RankedSpotCandidate(item, score, reasons))
        return sorted(ranked, key=lambda x: x.score, reverse=True)
