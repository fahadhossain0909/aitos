"""Outcome attribution for decision-fusion evidence components.

This module is deliberately read-only with respect to production policy. It
measures whether individual evidence sources (AMT/order flow/liquidity/etc.)
were directionally useful on closed decisions and produces bounded statistics
that a later policy optimizer can consume.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidencePerformance:
    source: str
    observations: int
    aligned_observations: int
    positive_outcomes: int
    negative_outcomes: int
    total_pnl: float
    average_r_multiple: float
    alignment_rate: float
    outcome_win_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observations": self.observations,
            "aligned_observations": self.aligned_observations,
            "positive_outcomes": self.positive_outcomes,
            "negative_outcomes": self.negative_outcomes,
            "total_pnl": self.total_pnl,
            "average_r_multiple": self.average_r_multiple,
            "alignment_rate": self.alignment_rate,
            "outcome_win_rate": self.outcome_win_rate,
        }


class DecisionEvidenceAttributor:
    """Join decision evidence contributions with realized trade outcomes."""

    @staticmethod
    def attribute(records: Iterable[Mapping[str, Any]]) -> list[EvidencePerformance]:
        decisions: dict[str, Mapping[str, Any]] = {}
        outcomes: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            kind = record.get("record_type")
            decision_id = str(record.get("decision_id") or "")
            if not decision_id:
                continue
            if kind == "DECISION":
                decisions[decision_id] = record
            elif kind == "OUTCOME":
                outcomes.setdefault(decision_id, []).append(record)

        buckets: dict[str, list[tuple[float, float]]] = {}
        for decision_id, decision in decisions.items():
            contribution_map = DecisionEvidenceAttributor._contributions(decision)
            for outcome in outcomes.get(decision_id, []):
                pnl = outcome.get("pnl")
                if not isinstance(pnl, (int, float)):
                    continue
                r_multiple = outcome.get("r_multiple")
                r_value = (
                    float(r_multiple) if isinstance(r_multiple, (int, float)) else 0.0
                )
                for source, score in contribution_map.items():
                    # A positive normalized contribution means evidence agreed
                    # with the recorded decision direction.
                    aligned = 1.0 if score > 0 else 0.0
                    buckets.setdefault(source, []).append(
                        (float(pnl), r_value * aligned)
                    )

        result: list[EvidencePerformance] = []
        for source, values in sorted(buckets.items()):
            pnls = [v[0] for v in values]
            rs = [v[1] for v in values]
            positive = sum(1 for p in pnls if p > 0)
            negative = sum(1 for p in pnls if p < 0)
            aligned = sum(1 for r in rs if r != 0.0)
            result.append(
                EvidencePerformance(
                    source=source,
                    observations=len(values),
                    aligned_observations=aligned,
                    positive_outcomes=positive,
                    negative_outcomes=negative,
                    total_pnl=sum(pnls),
                    average_r_multiple=sum(rs) / len(rs) if rs else 0.0,
                    alignment_rate=aligned / len(values) if values else 0.0,
                    outcome_win_rate=positive / len(pnls) if pnls else 0.0,
                )
            )
        return result

    @staticmethod
    def _contributions(decision: Mapping[str, Any]) -> dict[str, float]:
        raw = decision.get("evidence_contributions") or decision.get(
            "fusion_contributions"
        )
        if isinstance(raw, Mapping):
            result: dict[str, float] = {}
            for key, value in raw.items():
                if isinstance(value, (int, float)):
                    result[str(key)] = float(value)
                elif isinstance(value, Mapping) and isinstance(
                    value.get("score"), (int, float)
                ):
                    result[str(key)] = float(value["score"])
            return result
        if isinstance(raw, list):
            result = {}
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                source = item.get("source")
                score = item.get("score", item.get("weighted_score"))
                if source is not None and isinstance(score, (int, float)):
                    result[str(source)] = float(score)
            return result
        # Backward-compatible fallback: component_scores are still useful
        # attribution inputs when older decisions lack contribution metadata.
        scores = decision.get("component_scores")
        if isinstance(scores, Mapping):
            return {
                str(k): float(v)
                for k, v in scores.items()
                if isinstance(v, (int, float))
            }
        return {}
