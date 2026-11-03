"""Shadow evaluation for candidate evidence-fusion weights."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ShadowWeightResult:
    baseline_score: float
    candidate_score: float
    delta: float
    observations: int
    candidate_better: bool
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evaluate_weight_candidate(
    records: Iterable[Mapping[str, Any]],
    baseline_weights: Mapping[str, float],
    candidate_weights: Mapping[str, float],
    *,
    min_observations: int = 30,
    min_improvement: float = 0.0,
) -> ShadowWeightResult:
    decisions: dict[str, Mapping[str, Any]] = {}
    outcomes: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        decision_id = str(record.get("decision_id") or "")
        if not decision_id:
            continue
        if record.get("record_type") == "DECISION":
            decisions[decision_id] = record
        elif record.get("record_type") == "OUTCOME":
            outcomes.setdefault(decision_id, []).append(record)
    baseline_total = candidate_total = 0.0
    observations = 0
    for decision_id, decision in decisions.items():
        raw = decision.get("evidence_contributions") or decision.get(
            "fusion_contributions"
        )
        if not isinstance(raw, Mapping):
            continue
        contributions = {
            str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))
        }
        direction = str(decision.get("direction") or decision.get("side") or "").lower()
        sign = 1.0 if direction in {"long", "buy", "bullish", "up"} else -1.0
        for outcome in outcomes.get(decision_id, []):
            r = outcome.get("r_multiple")
            if not isinstance(r, (int, float)):
                continue
            bsignal = sum(
                float(baseline_weights.get(k, 0.0)) * v
                for k, v in contributions.items()
            )
            csignal = sum(
                float(candidate_weights.get(k, 0.0)) * v
                for k, v in contributions.items()
            )
            baseline_total += float(r) if bsignal * sign > 0 else -abs(float(r))
            candidate_total += float(r) if csignal * sign > 0 else -abs(float(r))
            observations += 1
    baseline_score = baseline_total / observations if observations else 0.0
    candidate_score = candidate_total / observations if observations else 0.0
    delta = candidate_score - baseline_score
    eligible = observations >= min_observations and delta >= min_improvement
    reason = (
        "eligible"
        if eligible
        else (
            "insufficient_observations"
            if observations < min_observations
            else "candidate_does_not_meet_improvement_threshold"
        )
    )
    return ShadowWeightResult(
        baseline_score,
        candidate_score,
        delta,
        observations,
        delta > 0,
        eligible,
        reason,
    )
