"""Trade Thesis data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ThesisHealth(str, Enum):
    """How well the current market agrees with the original entry thesis."""

    INTACT = "INTACT"  # thesis still valid
    DEGRADED = "DEGRADED"  # some confirmation lost, not yet invalid
    INVALIDATED = "INVALIDATED"  # core invalidation triggered


@dataclass(frozen=True)
class ThesisComponent:
    """One named reason the trade was taken (e.g. buyer_imbalance)."""

    code: str
    description: str
    weight: float = 1.0  # relative importance at entry

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "description": self.description,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class InvalidationCondition:
    """A condition that, if true, means the thesis is dead."""

    code: str
    description: str
    # Optional numeric threshold helpers for engines that evaluate them
    level: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "description": self.description,
            "level": self.level,
        }


@dataclass(frozen=True)
class ConfirmationSignal:
    """Evidence that should remain supportive while the thesis is alive."""

    code: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "description": self.description}


@dataclass(frozen=True)
class TradeThesis:
    """Canonical, machine-readable reason a position was opened."""

    trade_id: str
    symbol: str
    side: str  # LONG | SHORT
    entry_price: float
    components: tuple[ThesisComponent, ...]
    invalidations: tuple[InvalidationCondition, ...]
    confirmations: tuple[ConfirmationSignal, ...]
    expected_path_prices: tuple[float, ...] = ()  # ordered destinations A→B→C
    invalidation_price: float | None = None  # primary structural invalidation
    created_at: datetime | None = None
    notes: tuple[str, ...] = ()
    features: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "components": [c.to_dict() for c in self.components],
            "invalidations": [i.to_dict() for i in self.invalidations],
            "confirmations": [c.to_dict() for c in self.confirmations],
            "expected_path_prices": list(self.expected_path_prices),
            "invalidation_price": self.invalidation_price,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "notes": list(self.notes),
            "features": dict(self.features),
        }


@dataclass(frozen=True)
class ThesisEvaluation:
    """Result of checking current market against the original thesis."""

    health: ThesisHealth
    consistency_score: float  # 0–1 (1 = fully consistent)
    breached_invalidations: tuple[str, ...]
    lost_confirmations: tuple[str, ...]
    active_confirmations: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "health": self.health.value,
            "consistency_score": self.consistency_score,
            "breached_invalidations": list(self.breached_invalidations),
            "lost_confirmations": list(self.lost_confirmations),
            "active_confirmations": list(self.active_confirmations),
            "notes": list(self.notes),
        }
