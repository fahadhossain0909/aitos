"""Conditional hedge decision models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class HedgeAction(str, Enum):
    NONE = "NONE"
    OPEN = "OPEN"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class HedgeDecision:
    """Explainable temporary risk-overlay decision."""

    symbol: str
    primary_side: str
    action: HedgeAction
    hedge_side: str | None
    hedge_ratio: float
    hedge_score: float
    recovery_score: float
    reason_codes: tuple[str, ...]
    as_of: datetime
    features: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "primary_side": self.primary_side,
            "action": self.action.value,
            "hedge_side": self.hedge_side,
            "hedge_ratio": self.hedge_ratio,
            "hedge_score": self.hedge_score,
            "recovery_score": self.recovery_score,
            "reason_codes": list(self.reason_codes),
            "as_of": self.as_of.isoformat(),
            "features": dict(self.features),
            "notes": list(self.notes),
        }
