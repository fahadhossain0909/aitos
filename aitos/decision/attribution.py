"""Idempotent in-memory decision/outcome attribution contracts."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from .types import DecisionResult


@dataclass(frozen=True)
class DecisionOutcome:
    decision_id: str
    outcome_label: str
    realized_pnl: float
    fees: float
    funding: float
    slippage: float
    mae: float | None
    mfe: float | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.decision_id or not self.outcome_label:
            raise ValueError("decision and outcome identifiers are required")
        values = (self.realized_pnl, self.fees, self.funding, self.slippage)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("outcome values must be finite")
        if self.mae is not None and not math.isfinite(self.mae):
            raise ValueError("MAE must be finite")
        if self.mfe is not None and not math.isfinite(self.mfe):
            raise ValueError("MFE must be finite")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("outcome timestamp must be timezone-aware")


class DecisionAttributionStore(Protocol):
    def record_decision(self, decision: DecisionResult) -> None: ...
    def record_outcome(self, outcome: DecisionOutcome) -> None: ...


class InMemoryDecisionAttributionStore:
    """Test/shadow store; production persistence must implement the same contract."""

    def __init__(self) -> None:
        self._decisions: dict[str, DecisionResult] = {}
        self._outcomes: dict[str, DecisionOutcome] = {}
        self._lock = RLock()

    def record_decision(self, decision: DecisionResult) -> None:
        with self._lock:
            previous = self._decisions.get(decision.decision_id)
            if previous is not None and previous != decision:
                raise ValueError("decision id collision with different content")
            self._decisions[decision.decision_id] = decision

    def record_outcome(self, outcome: DecisionOutcome) -> None:
        with self._lock:
            if outcome.decision_id not in self._decisions:
                raise KeyError("outcome references an unknown decision")
            previous = self._outcomes.get(outcome.decision_id)
            if previous is not None and previous != outcome:
                raise ValueError("conflicting outcome already recorded")
            self._outcomes[outcome.decision_id] = outcome

    def get_decision(self, decision_id: str) -> DecisionResult | None:
        with self._lock:
            return self._decisions.get(decision_id)

    def get_outcome(self, decision_id: str) -> DecisionOutcome | None:
        with self._lock:
            return self._outcomes.get(decision_id)

    def snapshot(self) -> tuple[tuple[DecisionResult, ...], tuple[DecisionOutcome, ...]]:
        with self._lock:
            return tuple(self._decisions.values()), tuple(self._outcomes.values())


def stable_decision_id(symbol: str, snapshot_id: str, spec_name: str) -> str:
    """Build a deterministic identifier for a market-state decision key."""
    payload = "|".join((symbol, snapshot_id, spec_name))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
