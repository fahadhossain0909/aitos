"""Explicit event-impact champion/challenger governance facade.

Uses AITOS' existing durable StatisticalModelRegistry and fail-closed
StatisticalGovernanceRuntime. Candidate artifacts are references plus hashes;
this module never imports or executes serialized model code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aitos.statistics.governance import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
)
from aitos.statistics.governance_runtime import StatisticalGovernanceRuntime
from aitos.statistics.registry import ModelArtifact, StatisticalModelRegistry


@dataclass(frozen=True, slots=True)
class EventImpactCandidate:
    model_id: str
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    config_hash: str
    feature_schema_version: str
    metadata: Mapping[str, Any]


class EventImpactModelGovernance:
    """Drift-gated registry integration; operator approval is required per action."""

    def __init__(
        self,
        registry: StatisticalModelRegistry,
        *,
        policy: PromotionPolicy = PromotionPolicy(),
    ):
        self.registry = registry
        self.runtime = StatisticalGovernanceRuntime(registry, policy=policy)

    def register_challenger(self, candidate: EventImpactCandidate) -> bool:
        return self.registry.register_candidate(
            model_id=candidate.model_id,
            model_version=candidate.model_version,
            artifact_uri=candidate.artifact_uri,
            artifact_sha256=candidate.artifact_sha256,
            config_hash=candidate.config_hash,
            feature_schema_version=candidate.feature_schema_version,
            metadata=dict(candidate.metadata),
        )

    def observe_drift(self, reference, current):
        return self.runtime.observe_live_features(reference, current)

    def drift_unavailable(self, reason: str):
        return self.runtime.mark_drift_unavailable(reason)

    def evaluate(self, evidence: PromotionEvidence) -> PromotionDecision:
        return self.runtime.evaluate_challenger(evidence)

    def promote(
        self, evidence: PromotionEvidence, *, operator_approved: bool = False
    ) -> PromotionDecision:
        return self.runtime.promote_challenger(
            evidence, operator_approved=operator_approved
        )

    def rollback(
        self, model_id: str, target_version: str, *, operator_approved: bool = False
    ) -> ModelArtifact:
        if not operator_approved:
            raise PermissionError("explicit operator approval is required for rollback")
        return self.runtime.rollback(model_id, target_version, operator_approved=True)


__all__ = ["EventImpactCandidate", "EventImpactModelGovernance"]
