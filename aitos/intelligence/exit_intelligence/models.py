"""Exit-decision data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ExitAction(str, Enum):
    HOLD = "HOLD"
    MANAGE = "MANAGE"  # tighten stop / partial reduce / wait confirmation
    EXIT = "EXIT"


@dataclass(frozen=True)
class ExitReason:
    """A single scored contribution to the exit decision."""

    code: str
    description: str
    weight: float  # signed: positive → toward EXIT, negative → toward HOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "description": self.description,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class ExitDecision:
    """Full explainable exit decision for one open position."""

    symbol: str
    side: str
    action: ExitAction
    exit_score: float  # 0–1 (higher → more exit pressure)
    expected_remaining_edge: float  # signed; >0 favours HOLD
    reasons: tuple[ExitReason, ...]
    suggested_reduce_fraction: float  # 0–1, meaningful when action=MANAGE
    as_of: datetime
    features: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action.value,
            "exit_score": self.exit_score,
            "expected_remaining_edge": self.expected_remaining_edge,
            "reasons": [r.to_dict() for r in self.reasons],
            "suggested_reduce_fraction": self.suggested_reduce_fraction,
            "as_of": self.as_of.isoformat(),
            "features": dict(self.features),
            "notes": list(self.notes),
        }
