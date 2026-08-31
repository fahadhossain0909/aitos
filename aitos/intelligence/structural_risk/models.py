"""Structural-stop data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StructuralStop:
    """The price at which the trade thesis is considered invalid."""

    symbol: str
    side: str  # LONG | SHORT
    entry_price: float
    stop_price: float
    distance: float  # absolute
    distance_pct: float  # relative to entry
    invalidation_type: str  # structure_break | value_area | liquidity | swing | emergency_fallback
    confidence: float  # 0–1
    buffer_applied: float  # price units added for noise/liquidity
    as_of: datetime
    notes: tuple[str, ...] = ()
    features: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "distance": self.distance,
            "distance_pct": self.distance_pct,
            "invalidation_type": self.invalidation_type,
            "confidence": self.confidence,
            "buffer_applied": self.buffer_applied,
            "as_of": self.as_of.isoformat(),
            "notes": list(self.notes),
            "features": dict(self.features),
        }
