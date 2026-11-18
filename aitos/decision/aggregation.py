"""Correlation-aware aggregation of independent decision assessments."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from .types import AgentAssessment


@dataclass(frozen=True)
class AggregationResult:
    probabilities: Mapping[str, float]
    disagreement: float
    effective_agent_count: float
    contributing_agents: tuple[str, ...]


def _validated_distribution(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        raise ValueError("probability distribution cannot be empty")
    result = {}
    for key, value in values.items():
        if not key or not math.isfinite(value) or value < 0:
            raise ValueError("probabilities must be finite and non-negative")
        result[key] = float(value)
    total = sum(result.values())
    if total <= 0 or abs(total - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to one")
    return result


def aggregate_assessments(
    assessments: Sequence[AgentAssessment],
) -> AggregationResult:
    """Discount duplicate correlation groups; this is a heuristic, not independence proof."""
    if not assessments:
        raise ValueError("at least one assessment is required")
    distributions = [
        (item, _validated_distribution(item.probabilities))
        for item in assessments
        if item.reliability > 0
    ]
    if not distributions:
        raise ValueError("no assessments have positive reliability")
    actions = set(distributions[0][1])
    if any(set(values) != actions for _, values in distributions):
        raise ValueError("all assessments must use the same outcome labels")

    groups: dict[str, list[tuple[AgentAssessment, dict[str, float]]]] = {}
    for index, pair in enumerate(distributions):
        assessment = pair[0]
        key = assessment.correlation_group or f"__independent_{index}"
        groups.setdefault(key, []).append(pair)

    weighted = []
    for members in groups.values():
        group_budget = max(item.reliability for item, _ in members)
        member_total = sum(item.reliability for item, _ in members)
        for assessment, distribution in members:
            weight = group_budget * assessment.reliability / member_total
            weighted.append((distribution, weight))

    total_weight = sum(weight for _, weight in weighted)
    probabilities = {
        action: sum(values[action] * weight for values, weight in weighted)
        / total_weight
        for action in actions
    }
    disagreement = max(
        max(values[action] for _, values in distributions)
        - min(values[action] for _, values in distributions)
        for action in actions
    )
    squared_weights = sum((weight / total_weight) ** 2 for _, weight in weighted)
    return AggregationResult(
        probabilities,
        disagreement,
        1.0 / squared_weights,
        tuple(item.agent for item, _ in distributions),
    )
