"""Opt-in live drift and champion/challenger governance controller."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .governance import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from .registry import ModelArtifact, StatisticalModelRegistry
from .validation import DriftReport, assess_feature_drift


@dataclass(frozen=True)
class DriftAction:
    report: DriftReport
    status: str
    promotion_blocked: bool
    reasons: tuple[str, ...]


class StatisticalGovernanceRuntime:
    """Runtime-safe drift assessment and explicit champion/challenger control.

    The caller supplies timestamp-aligned reference and current feature batches.
    Drift blocks promotion but does not autonomously close positions, disable
    execution, or replace the champion. Model promotion requires a second,
    explicit operator approval at this boundary.
    """

    def __init__(
        self,
        registry: StatisticalModelRegistry,
        *,
        policy: PromotionPolicy = PromotionPolicy(),
        drift_warning_threshold: float = 0.10,
        drift_critical_threshold: float = 0.25,
    ):
        if not 0 < drift_warning_threshold < drift_critical_threshold:
            raise ValueError("drift thresholds must be positive and ordered")
        self.registry = registry
        self.policy = policy
        self.drift_warning_threshold = drift_warning_threshold
        self.drift_critical_threshold = drift_critical_threshold
        self._last_drift: DriftAction | None = None

    @property
    def last_drift(self) -> DriftAction | None:
        return self._last_drift

    def observe_live_features(
        self,
        reference: Mapping[str, Sequence[float]],
        current: Mapping[str, Sequence[float]],
    ) -> DriftAction:
        report = assess_feature_drift(
            reference,
            current,
            threshold=self.drift_warning_threshold,
        )
        if report.max_psi >= self.drift_critical_threshold:
            status = "CRITICAL"
            reasons = ("critical_feature_drift",)
        elif report.drifted_features:
            status = "WARNING"
            reasons = ("feature_drift_detected",)
        else:
            status = "NORMAL"
            reasons = ()
        action = DriftAction(
            report=report,
            status=status,
            promotion_blocked=status != "NORMAL",
            reasons=reasons,
        )
        self._last_drift = action
        return action

    def evaluate_challenger(
        self, evidence: PromotionEvidence
    ) -> PromotionDecision:
        decision = evaluate_promotion(evidence, self.policy)
        if self._last_drift is not None and self._last_drift.promotion_blocked:
            reasons = tuple(dict.fromkeys(decision.reasons + ("live_feature_drift_blocks_promotion",)))
            return PromotionDecision(False, reasons)
        return decision

    def promote_challenger(
        self,
        evidence: PromotionEvidence,
        *,
        operator_approved: bool = False,
    ) -> PromotionDecision:
        """Promote only when both evidence and this invocation carry approval."""
        approved = bool(operator_approved and evidence.operator_approved)
        candidate = replace(evidence, operator_approved=approved)
        decision = self.evaluate_challenger(candidate)
        if not decision.eligible:
            # Persist the blocked decision through the registry's normal audit
            # path, even when the caller has not approved the operation.
            return self.registry.promote(candidate, policy=self.policy)
        artifact = self.registry.promote(candidate, policy=self.policy)
        return artifact

    def rollback(
        self, model_id: str, target_version: str, *, operator_approved: bool = False
    ) -> ModelArtifact:
        return self.registry.rollback(
            model_id, target_version, operator_approved=operator_approved
        )
