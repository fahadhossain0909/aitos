"""Uncertainty-aware ensemble of heterogeneous statistical forecasts."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ForecastMember:
    model_id: str
    weight: float
    expected_return: float
    variance: float
    probability_up: float


@dataclass(frozen=True)
class EnsembleForecast:
    expected_return: float
    variance: float
    volatility: float
    probability_up: float
    return_disagreement: float
    member_count: int
    normalized_weights: tuple[float, ...]


def combine_forecasts(members: Iterable[ForecastMember]) -> EnsembleForecast:
    """Combine forecast means, within-model variance and between-model spread.

    The predictive variance follows the law of total variance: weighted
    within-model variance plus weighted dispersion of model means. This keeps
    disagreement visible instead of averaging it away.
    """
    items = tuple(members)
    if not items:
        raise ValueError("at least one forecast member is required")
    for item in items:
        values = (
            item.weight,
            item.expected_return,
            item.variance,
            item.probability_up,
        )
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("forecast members must contain finite values")
        if not item.model_id.strip():
            raise ValueError("model_id must not be empty")
        if item.weight < 0 or item.variance < 0:
            raise ValueError("weights and variances must be non-negative")
        if not 0.0 <= item.probability_up <= 1.0:
            raise ValueError("probability_up must be in [0, 1]")
    total_weight = sum(item.weight for item in items)
    if total_weight <= 0:
        raise ValueError("forecast weights must have positive mass")
    weights = tuple(item.weight / total_weight for item in items)
    expected_return = sum(
        weight * item.expected_return for weight, item in zip(weights, items)
    )
    within_variance = sum(
        weight * item.variance for weight, item in zip(weights, items)
    )
    disagreement_variance = sum(
        weight * (item.expected_return - expected_return) ** 2
        for weight, item in zip(weights, items)
    )
    variance = max(0.0, within_variance + disagreement_variance)
    probability_up = sum(
        weight * item.probability_up for weight, item in zip(weights, items)
    )
    return EnsembleForecast(
        expected_return=expected_return,
        variance=variance,
        volatility=math.sqrt(variance),
        probability_up=probability_up,
        return_disagreement=math.sqrt(disagreement_variance),
        member_count=len(items),
        normalized_weights=weights,
    )
