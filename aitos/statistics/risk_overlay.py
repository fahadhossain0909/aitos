"""Statistical risk overlay contracts; never replaces hard risk controls."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StatisticalRiskAssessment:
    size_multiplier: float
    volatility_ratio: float
    tail_probability: float
    expected_shortfall: float | None
    reasons: tuple[str, ...]


def assess_statistical_risk(*, forecast_volatility: float,
                            reference_volatility: float,
                            tail_probability: float,
                            expected_shortfall: float | None = None,
                            min_multiplier: float = 0.20,
                            max_multiplier: float = 1.0) -> StatisticalRiskAssessment:
    """Conservative sizing multiplier from volatility and tail-risk forecasts.

    The multiplier only reduces a separately calculated base size. It cannot
    increase size above 1, waive a limit, or approve an order. Callers must
    still enforce RiskLimits, freshness, liquidity, leverage and circuit
    breakers. Forecasts must be calibrated for the same symbol/horizon.
    """
    values = (forecast_volatility, reference_volatility, tail_probability,
              min_multiplier, max_multiplier)
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError("risk inputs must be finite")
    if forecast_volatility < 0 or reference_volatility <= 0:
        raise ValueError("volatility inputs are invalid")
    if not 0 <= tail_probability <= 1:
        raise ValueError("tail_probability must be in [0, 1]")
    if not 0 < min_multiplier <= max_multiplier <= 1:
        raise ValueError("multipliers must satisfy 0 < min <= max <= 1")
    if expected_shortfall is not None and (
        not math.isfinite(float(expected_shortfall)) or expected_shortfall < 0
    ):
        raise ValueError("expected_shortfall must be finite and non-negative")
    ratio = forecast_volatility / reference_volatility
    # Square-root volatility scaling is less aggressive than inverse variance.
    vol_factor = 1.0 / math.sqrt(max(1.0, ratio))
    # Tail risk is a separate penalty; no tail probability is treated as a
    # deterministic loss probability or as a VaR estimate.
    tail_factor = max(0.25, 1.0 - 0.75 * tail_probability)
    multiplier = max(min_multiplier, min(max_multiplier, vol_factor * tail_factor))
    reasons = []
    if ratio > 1.0:
        reasons.append("forecast_volatility_above_reference")
    if tail_probability >= 0.05:
        reasons.append("elevated_tail_probability")
    if expected_shortfall is not None and expected_shortfall > 0:
        reasons.append("expected_shortfall_available_for_limit_checks")
    return StatisticalRiskAssessment(
        multiplier, ratio, tail_probability, expected_shortfall, tuple(reasons)
    )
