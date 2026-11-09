"""Explicit, evidence-gated statistical model promotion policy."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionPolicy:
    min_observations: int = 1000
    min_shadow_days: int = 30
    max_brier: float = 0.22
    max_qlike: float = 2.0
    max_pbo: float = 0.20
    max_drawdown: float = 0.10
    max_drift_score: float = 0.10
    require_operator_approval: bool = True

    def __post_init__(self) -> None:
        if self.min_observations <= 0 or self.min_shadow_days <= 0:
            raise ValueError("minimum sample size and shadow duration must be positive")
        limits = (
            self.max_brier,
            self.max_qlike,
            self.max_pbo,
            self.max_drawdown,
            self.max_drift_score,
        )
        if any(not math.isfinite(value) or value < 0 for value in limits):
            raise ValueError("promotion limits must be finite and non-negative")
        if self.max_pbo > 1.0 or self.max_brier > 1.0:
            raise ValueError("max_pbo and max_brier must be in [0, 1]")


@dataclass(frozen=True)
class PromotionEvidence:
    model_id: str
    model_version: str
    observations: int
    shadow_days: float
    brier: float
    qlike: float
    pbo: float
    max_drawdown: float
    drift_score: float
    paired_oos_improvement_lower_bound: float
    operator_approved: bool = False


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_promotion(
    evidence: PromotionEvidence,
    policy: PromotionPolicy = PromotionPolicy(),
) -> PromotionDecision:
    """Evaluate promotion eligibility; never performs promotion itself.

    paired_oos_improvement_lower_bound must be the lower confidence bound
    from a paired, leakage-aware out-of-sample bootstrap against the incumbent.
    A positive point estimate alone is insufficient. The caller remains
    responsible for artifact registration, independent review and rollback.
    """
    if not evidence.model_id.strip() or not evidence.model_version.strip():
        raise ValueError("model identity is required")
    if evidence.observations < 0 or evidence.shadow_days < 0:
        raise ValueError("sample count and shadow duration must be non-negative")
    metrics = (
        evidence.shadow_days,
        evidence.brier,
        evidence.qlike,
        evidence.pbo,
        evidence.max_drawdown,
        evidence.drift_score,
        evidence.paired_oos_improvement_lower_bound,
    )
    if any(not math.isfinite(float(value)) for value in metrics):
        raise ValueError("promotion evidence must contain finite metrics")
    if not 0.0 <= evidence.pbo <= 1.0:
        raise ValueError("pbo must be in [0, 1]")
    if not 0.0 <= evidence.brier <= 1.0:
        raise ValueError("brier must be in [0, 1]")
    if (
        min(
            evidence.brier,
            evidence.qlike,
            evidence.max_drawdown,
            evidence.drift_score,
        )
        < 0
    ):
        raise ValueError("loss, drawdown and drift metrics must be non-negative")

    reasons = []
    if evidence.observations < policy.min_observations:
        reasons.append("insufficient_out_of_sample_observations")
    if evidence.shadow_days < policy.min_shadow_days:
        reasons.append("insufficient_shadow_duration")
    if evidence.brier > policy.max_brier:
        reasons.append("brier_above_policy_limit")
    if evidence.qlike > policy.max_qlike:
        reasons.append("qlike_above_policy_limit")
    if evidence.pbo > policy.max_pbo:
        reasons.append("probability_of_backtest_overfitting_above_limit")
    if evidence.max_drawdown > policy.max_drawdown:
        reasons.append("drawdown_above_policy_limit")
    if evidence.drift_score > policy.max_drift_score:
        reasons.append("model_or_feature_drift_above_limit")
    if evidence.paired_oos_improvement_lower_bound <= 0:
        reasons.append("out_of_sample_improvement_not_statistically_positive")
    if policy.require_operator_approval and not evidence.operator_approved:
        reasons.append("explicit_operator_approval_required")
    return PromotionDecision(not reasons, tuple(reasons))
