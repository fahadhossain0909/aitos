"""Chronological, leakage-aware ADL replay and model-governance metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .calibration import CalibrationMetrics, ProbabilityCalibrator
from .runtime import ADLRuntime
from .types import DecisionAction, DecisionResult, DecisionSpec, MarketSnapshot


@dataclass(frozen=True)
class ReplayObservation:
    snapshot: MarketSnapshot
    realized_action: str
    matured_at: datetime

    def __post_init__(self) -> None:
        if self.matured_at.tzinfo is None or self.matured_at.utcoffset() is None:
            raise ValueError("replay outcome maturity timestamp must be timezone-aware")
        if self.realized_action not in {"LONG", "SHORT", "NO_TRADE"}:
            raise ValueError("unsupported replay outcome label")
        if self.matured_at < self.snapshot.timestamp:
            raise ValueError("outcome cannot mature before its market snapshot")


@dataclass(frozen=True)
class ReplayReport:
    observations: int
    evaluated: int
    abstentions: int
    proposed_action_accuracy: float | None
    calibration: CalibrationMetrics
    results: tuple[DecisionResult, ...]


def replay_chronologically(runtime: ADLRuntime, spec: DecisionSpec, observations: Iterable[ReplayObservation], calibrator: ProbabilityCalibrator) -> ReplayReport:
    """Evaluate in event-time order; only mature labels before each later decision."""
    ordered = sorted(observations, key=lambda item: (item.snapshot.timestamp, item.matured_at, item.snapshot.symbol))
    results: list[DecisionResult] = []
    correct = 0
    abstentions = 0
    pending: list[ReplayObservation] = []
    for item in ordered:
        decision_time = item.snapshot.timestamp
        matured, pending = (
            [row for row in pending if row.matured_at <= decision_time],
            [row for row in pending if row.matured_at > decision_time],
        )
        for matured_item in matured:
            for action in ("LONG", "SHORT", "NO_TRADE"):
                probability = matured_item._raw_probabilities.get(action)
                if probability is not None:
                    calibrator.update(
                        action,
                        probability,
                        action == matured_item.realized_action,
                    )
        result = runtime.evaluate(item.snapshot, spec, now=decision_time)
        results.append(result)
        abstentions += int(result.action == DecisionAction.ABSTAIN)
        correct += int(result.proposed_action.value == item.realized_action)
        object.__setattr__(item, "_raw_probabilities", dict(result.raw_probabilities))
        pending.append(item)
    count = len(ordered)
    return ReplayReport(count, count, abstentions, correct / count if count else None, calibrator.metrics(), tuple(results))


@dataclass(frozen=True)
class GovernanceThresholds:
    maximum_brier_score: float = 0.25
    maximum_ece: float = 0.10
    maximum_abstention_rate: float = 0.95
    minimum_samples: int = 100


@dataclass(frozen=True)
class GovernanceAssessment:
    eligible_for_shadow: bool
    eligible_for_canary: bool
    reasons: tuple[str, ...]


def assess_challenger(metrics: CalibrationMetrics, *, abstention_rate: float, thresholds: GovernanceThresholds | None = None) -> GovernanceAssessment:
    """Assess evidence quality; never auto-promotes or enables execution."""
    limits = thresholds or GovernanceThresholds()
    if not 0.0 <= abstention_rate <= 1.0:
        raise ValueError("abstention rate must be between zero and one")
    reasons: list[str] = []
    if metrics.samples < limits.minimum_samples:
        reasons.append("insufficient matured out-of-sample observations")
    if metrics.brier_score is None or metrics.brier_score > limits.maximum_brier_score:
        reasons.append("Brier score is unavailable or exceeds governance threshold")
    if metrics.expected_calibration_error is None or metrics.expected_calibration_error > limits.maximum_ece:
        reasons.append("calibration error is unavailable or exceeds threshold")
    if abstention_rate > limits.maximum_abstention_rate:
        reasons.append("abstention rate exceeds governance threshold")
    return GovernanceAssessment(metrics.samples >= limits.minimum_samples, not reasons, tuple(reasons))
