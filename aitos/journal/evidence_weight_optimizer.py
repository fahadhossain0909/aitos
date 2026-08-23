"""Bounded candidate fusion-weight generation from evidence performance.

This module is intentionally proposal-only: it never mutates the live fusion
engine. Weights are derived from historical evidence quality and normalized
back to the configured total, with per-component movement bounded around the
base weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping

from aitos.journal.evidence_attribution import EvidencePerformance


@dataclass(frozen=True)
class EvidenceWeightCandidate:
    candidate_id: str
    base_policy_version: str
    weights: Mapping[str, float]
    sample_counts: Mapping[str, int]
    confidence: Mapping[str, float]
    max_relative_change: float

    def to_dict(self):
        return {
            "candidate_id": self.candidate_id,
            "base_policy_version": self.base_policy_version,
            "weights": dict(self.weights),
            "sample_counts": dict(self.sample_counts),
            "confidence": dict(self.confidence),
            "max_relative_change": self.max_relative_change,
        }


class EvidenceWeightOptimizer:
    """Turn evidence statistics into a bounded, shadow-testable candidate."""

    def __init__(
        self,
        *,
        min_observations: int = 30,
        max_relative_change: float = 0.20,
        min_weight: float = 0.02,
        max_weight: float = 0.30,
    ) -> None:
        if min_observations < 1 or not 0 < max_relative_change <= 1:
            raise ValueError("invalid optimizer bounds")
        if not 0 < min_weight <= max_weight:
            raise ValueError("invalid weight bounds")
        self.min_observations = min_observations
        self.max_relative_change = max_relative_change
        self.min_weight = min_weight
        self.max_weight = max_weight

    def propose(
        self,
        base_weights: Mapping[str, float],
        performance: Iterable[EvidencePerformance],
        candidate_id: str,
        base_policy_version: str = "fusion-v1",
    ) -> EvidenceWeightCandidate:
        base = {str(k): float(v) for k, v in base_weights.items() if float(v) >= 0}
        if not base or sum(base.values()) <= 0:
            raise ValueError("base_weights must contain positive total weight")
        stats = {
            p.source: p for p in performance if p.observations >= self.min_observations
        }
        raw = dict(base)
        confidence: Dict[str, float] = {}
        for source, weight in base.items():
            p = stats.get(source)
            if p is None:
                confidence[source] = 0.0
                continue
            # Blend alignment and realized R. The score is deliberately small;
            # the bounded change is the primary safety control.
            quality = 0.5 * p.alignment_rate + 0.5 * max(
                0.0, min(1.0, 0.5 + p.average_r_multiple)
            )
            confidence[source] = min(
                1.0, p.observations / float(self.min_observations * 5)
            )
            adjustment = (
                1.0
                + self.max_relative_change
                * ((quality - 0.5) * 2.0)
                * confidence[source]
            )
            raw[source] = weight * adjustment

        total = sum(raw.values())
        target_total = sum(base.values())
        normalized = {k: v / total * target_total for k, v in raw.items()}
        bounded = {}
        for source, weight in base.items():
            lower = max(self.min_weight, weight * (1.0 - self.max_relative_change))
            upper = min(self.max_weight, weight * (1.0 + self.max_relative_change))
            bounded[source] = min(upper, max(lower, normalized[source]))

        # Renormalize while preserving bounds as closely as possible. With the
        # default constraints this converges in one pass for ordinary policies.
        bounded_total = sum(bounded.values())
        if bounded_total > 0:
            bounded = {k: v / bounded_total * target_total for k, v in bounded.items()}

        return EvidenceWeightCandidate(
            candidate_id=candidate_id,
            base_policy_version=base_policy_version,
            weights={k: round(v, 6) for k, v in bounded.items()},
            sample_counts={
                k: (stats[k].observations if k in stats else 0) for k in base
            },
            confidence={k: round(v, 4) for k, v in confidence.items()},
            max_relative_change=self.max_relative_change,
        )
