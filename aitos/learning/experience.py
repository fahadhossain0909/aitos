"""Canonical experience records emitted by backtest, paper, and live stages."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ExperienceRecord:
    """Immutable decision/outcome record suitable for replay and learning."""

    timestamp: datetime
    source: str  # backtest | paper | live
    symbol: str
    decision: str
    outcome: str | None = None
    reward: float = 0.0
    confidence: float = 0.0
    quantity: float = 0.0
    price: float | None = None
    features: dict[str, Any] = field(default_factory=dict)
    market_state: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    strategy_version: str = "unknown"
    model_version: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
    experience_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.source not in {"backtest", "paper", "live"}:
            raise ValueError("source must be backtest, paper, or live")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.decision:
            raise ValueError("decision is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["timestamp"] = self.timestamp.astimezone(timezone.utc).isoformat()
        return value

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, default=str)
