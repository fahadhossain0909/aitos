"""Deterministic diagnostics for scanner score construction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreContribution:
    name: str
    raw_score: float
    weight: float
    weighted_contribution: float


def build_score_breakdown(
    component_scores: dict[str, float], weights: dict[str, float]
) -> tuple[list[ScoreContribution], float, float]:
    """Return per-component contribution, weight total, and normalized score.

    Component scores remain on the scanner's existing 0-10 scale. The returned
    normalized score remains on the existing 0-100 scale; this helper does not
    change threshold or scoring semantics.
    """
    contributions = [
        ScoreContribution(
            name=name,
            raw_score=float(score),
            weight=float(weights.get(name, 0.0)),
            weighted_contribution=float(score) * float(weights.get(name, 0.0)),
        )
        for name, score in component_scores.items()
    ]
    weight_total = sum(item.weight for item in contributions)
    weighted_sum = sum(item.weighted_contribution for item in contributions)
    normalized_score = weighted_sum / weight_total * 10.0 if weight_total else 0.0
    return contributions, weight_total, normalized_score
