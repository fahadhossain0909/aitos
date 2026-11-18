"""Chronological, leakage-aware ADL replay and model-governance metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .calibration import CalibrationMetrics, ProbabilityCalibrator
from .runtime import ADLRuntime
from .types import DecisionAction, DecisionResult, DecisionSpec, MarketSnapshot


@dataclass(frozen=True)
class ReplayExecution:
    """Observed execution economics for one replayed candidate/trade.

    Costs are quote-currency amounts. Funding is signed (negative means a
    rebate); the remaining cost fields are non-negative. Fill ratio records
    observed execution completeness and is never synthetically inferred.
    """

    gross_pnl: float
    fees: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0
    latency_cost: float = 0.0
    fill_ratio: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.gross_pnl,
            self.fees,
            self.funding,
            self.slippage,
            self.latency_cost,
            self.fill_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("replay execution values must be finite")
        if min(self.fees, self.slippage, self.latency_cost) < 0:
            raise ValueError("execution costs cannot be negative")
        if not 0.0 <= self.fill_ratio <= 1.0:
            raise ValueError("execution fill ratio must be between zero and one")

    @property
    def net_pnl(self) -> float:
        return (
            self.gross_pnl
            - self.fees
            - self.funding
            - self.slippage
            - self.latency_cost
        )


@dataclass(frozen=True)
class ReplayExecutionSummary:
    observations: int
    gross_pnl: float
    fees: float
    funding: float
    slippage: float
    latency_cost: float
    net_pnl: float
    partial_fill_observations: int
    mean_fill_ratio: float


@dataclass(frozen=True)
class ReplayObservation:
    snapshot: MarketSnapshot
    realized_action: str
    matured_at: datetime
    execution: ReplayExecution | None = None

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
    execution_summary: ReplayExecutionSummary | None = None


def replay_chronologically(
    runtime: ADLRuntime,
    spec: DecisionSpec,
    observations: Iterable[ReplayObservation],
    calibrator: ProbabilityCalibrator,
) -> ReplayReport:
    """Evaluate in event-time order; only mature labels before each later decision."""
    ordered = sorted(
        observations,
        key=lambda item: (
            item.snapshot.timestamp,
            item.matured_at,
            item.snapshot.symbol,
        ),
    )
    runtime.calibrator = calibrator
    results: list[DecisionResult] = []
    pending: list[tuple[ReplayObservation, dict[str, float]]] = []
    correct = 0
    abstentions = 0

    for item in ordered:
        decision_time = item.snapshot.timestamp
        matured = [row for row in pending if row[0].matured_at <= decision_time]
        pending = [row for row in pending if row[0].matured_at > decision_time]
        for matured_item, raw_probabilities in matured:
            for action in ("LONG", "SHORT", "NO_TRADE"):
                probability = raw_probabilities.get(action)
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
        pending.append((item, dict(result.raw_probabilities)))

    # Final labels can be scored after the final decision; they cannot affect it.
    for matured_item, raw_probabilities in pending:
        for action in ("LONG", "SHORT", "NO_TRADE"):
            probability = raw_probabilities.get(action)
            if probability is not None:
                calibrator.update(
                    action,
                    probability,
                    action == matured_item.realized_action,
                )

    count = len(ordered)
    executions = [item.execution for item in ordered if item.execution is not None]
    execution_summary = None
    if executions:
        execution_summary = ReplayExecutionSummary(
            observations=len(executions),
            gross_pnl=sum(item.gross_pnl for item in executions),
            fees=sum(item.fees for item in executions),
            funding=sum(item.funding for item in executions),
            slippage=sum(item.slippage for item in executions),
            latency_cost=sum(item.latency_cost for item in executions),
            net_pnl=sum(item.net_pnl for item in executions),
            partial_fill_observations=sum(item.fill_ratio < 1.0 for item in executions),
            mean_fill_ratio=sum(item.fill_ratio for item in executions) / len(executions),
        )
    return ReplayReport(
        observations=count,
        evaluated=count,
        abstentions=abstentions,
        proposed_action_accuracy=correct / count if count else None,
        calibration=calibrator.metrics(),
        results=tuple(results),
        execution_summary=execution_summary,
    )


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


def assess_challenger(
    metrics: CalibrationMetrics,
    *,
    abstention_rate: float,
    thresholds: GovernanceThresholds | None = None,
) -> GovernanceAssessment:
    """Assess evidence quality; never auto-promotes or enables execution."""
    limits = thresholds or GovernanceThresholds()
    if not 0.0 <= abstention_rate <= 1.0:
        raise ValueError("abstention rate must be between zero and one")
    reasons: list[str] = []
    if metrics.samples < limits.minimum_samples:
        reasons.append("insufficient matured out-of-sample observations")
    if metrics.brier_score is None or metrics.brier_score > limits.maximum_brier_score:
        reasons.append("Brier score is unavailable or exceeds governance threshold")
    if (
        metrics.expected_calibration_error is None
        or metrics.expected_calibration_error > limits.maximum_ece
    ):
        reasons.append("calibration error is unavailable or exceeds threshold")
    if abstention_rate > limits.maximum_abstention_rate:
        reasons.append("abstention rate exceeds governance threshold")
    return GovernanceAssessment(
        metrics.samples >= limits.minimum_samples, not reasons, tuple(reasons)
    )
