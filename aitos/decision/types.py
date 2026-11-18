"""Typed, provider-neutral contracts for AITOS Decision Language (ADL)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class DecisionAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class ChoiceQuestion:
    name: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or len(self.options) < 2:
            raise ValueError("choice questions require a name and at least two options")
        if len(set(self.options)) != len(self.options):
            raise ValueError("choice options must be unique")


@dataclass(frozen=True)
class ScoreQuestion:
    name: str
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if not self.name or self.minimum >= self.maximum:
            raise ValueError("score questions require a name and an increasing range")


@dataclass(frozen=True)
class NoulQuestion:
    name: str
    statement: str

    def __post_init__(self) -> None:
        if not self.name or not self.statement.strip():
            raise ValueError("noul questions require a name and a statement")


Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


@dataclass(frozen=True)
class DecisionSpec:
    name: str
    state_fields: tuple[str, ...]
    questions: tuple[Question, ...]
    outputs: tuple[str, ...] = ("decision", "probabilities", "reasons")

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("decision spec name is required")
        if len(set(self.state_fields)) != len(self.state_fields):
            raise ValueError("state fields must be unique")
        names = [question.name for question in self.questions]
        if len(set(names)) != len(names):
            raise ValueError("question names must be unique")
        if not self.questions:
            raise ValueError("at least one decision question is required")


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    features: Mapping[str, Any]
    source_versions: Mapping[str, str] = field(default_factory=dict)
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("snapshot symbol is required")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        if not isinstance(self.features, Mapping):
            raise ValueError("snapshot features must be a mapping")

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("current time must be timezone-aware")
        return (current - self.timestamp).total_seconds()


@dataclass(frozen=True)
class AgentAssessment:
    agent: str
    probabilities: Mapping[str, float]
    reliability: float = 1.0
    correlation_group: str | None = None
    evidence_ids: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("agent name is required")
        if not math.isfinite(self.reliability) or not 0.0 <= self.reliability <= 1.0:
            raise ValueError("agent reliability must be finite and between zero and one")


@dataclass(frozen=True)
class TradeEconomics:
    win_probability: float
    average_win: float
    average_loss: float
    costs: float

    def __post_init__(self) -> None:
        values = (self.win_probability, self.average_win, self.average_loss, self.costs)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("trade economics must contain finite values")
        if not 0.0 <= self.win_probability <= 1.0:
            raise ValueError("win probability must be between zero and one")
        if min(self.average_win, self.average_loss, self.costs) < 0:
            raise ValueError("win, loss and cost magnitudes cannot be negative")

    @property
    def expected_value(self) -> float:
        return (
            self.win_probability * self.average_win
            - (1.0 - self.win_probability) * self.average_loss
            - self.costs
        )


@dataclass(frozen=True)
class ModelResponse:
    probabilities: Mapping[str, float]
    scores: Mapping[str, float] = field(default_factory=dict)
    nouls: Mapping[str, float] = field(default_factory=dict)
    assessments: tuple[AgentAssessment, ...] = ()
    economics: TradeEconomics | None = None
    model_id: str = "unspecified"
    model_version: str = "unspecified"
    latency_ms: float | None = None
    rationale: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model_id or not self.model_version:
            raise ValueError("model identity and version are required")
        if self.latency_ms is not None and (
            not math.isfinite(self.latency_ms) or self.latency_ms < 0
        ):
            raise ValueError("model latency must be finite and non-negative")


@dataclass(frozen=True)
class DecisionPolicy:
    minimum_probability: float = 0.60
    minimum_margin: float = 0.10
    maximum_disagreement: float = 0.35
    maximum_snapshot_age_seconds: float = 5.0
    require_calibration: bool = True
    require_positive_expected_value: bool = True
    shadow_mode: bool = True
    hard_veto_features: tuple[str, ...] = (
        "risk_veto", "execution_veto", "data_integrity_veto",
    )

    def __post_init__(self) -> None:
        values = (
            self.minimum_probability, self.minimum_margin, self.maximum_disagreement,
            self.maximum_snapshot_age_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("decision thresholds must be finite")
        if not 0.0 <= self.minimum_probability <= 1.0:
            raise ValueError("minimum probability must be between zero and one")
        if not 0.0 <= self.minimum_margin <= 1.0:
            raise ValueError("minimum margin must be between zero and one")
        if not 0.0 <= self.maximum_disagreement <= 1.0:
            raise ValueError("maximum disagreement must be between zero and one")
        if self.maximum_snapshot_age_seconds <= 0:
            raise ValueError("maximum snapshot age must be positive")


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    symbol: str
    action: DecisionAction
    proposed_action: DecisionAction
    probabilities: Mapping[str, float]
    raw_probabilities: Mapping[str, float]
    calibrated: bool
    evidence_disagreement: float | None
    expected_value: float | None
    reasons: tuple[str, ...]
    model_id: str
    model_version: str
    snapshot_id: str
    evaluated_at: datetime
    shadow_mode: bool = True
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        if self.execution_eligible:
            raise ValueError("ADL decisions are advisory and cannot authorize execution")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("decision timestamp must be timezone-aware")
