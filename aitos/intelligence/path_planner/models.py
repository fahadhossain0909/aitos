"""Path-plan data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PathDestination:
    """A single probable price level the market may reach."""

    price: float
    probability: float  # 0–1
    distance: float  # absolute price distance from current
    market_structure_type: str  # LVN | HVN | POC | prior_high | prior_low | liquidity_pool | swing | vah | val
    liquidity_type: str  # resting | stop_cluster | equal_highs | equal_lows | none
    expected_horizon: str  # scalp | intraday | swing
    confidence: float  # 0–1 how reliable this destination estimate is
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "probability": self.probability,
            "distance": self.distance,
            "market_structure_type": self.market_structure_type,
            "liquidity_type": self.liquidity_type,
            "expected_horizon": self.expected_horizon,
            "confidence": self.confidence,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PathPlan:
    """Ranked upside and downside destinations from the current price."""

    symbol: str
    current_price: float
    upside: tuple[PathDestination, ...]
    downside: tuple[PathDestination, ...]
    as_of: datetime
    features: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "upside": [d.to_dict() for d in self.upside],
            "downside": [d.to_dict() for d in self.downside],
            "as_of": self.as_of.isoformat(),
            "features": dict(self.features),
            "notes": list(self.notes),
        }

    @property
    def nearest_upside(self) -> PathDestination | None:
        return self.upside[0] if self.upside else None

    @property
    def nearest_downside(self) -> PathDestination | None:
        return self.downside[0] if self.downside else None
