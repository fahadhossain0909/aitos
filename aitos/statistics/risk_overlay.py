"""Statistical risk overlay contracts; never replaces hard risk controls."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class StatisticalRiskAssessment:
    size_multiplier: float
    volatility_ratio: float
    tail_probability: float
    expected_shortfall: float | None
    reasons: tuple[str, ...]


def assess_statistical_risk(
    *,
    forecast_volatility: float,
    reference_volatility: float,
    tail_probability: float,
    expected_shortfall: float | None = None,
    min_multiplier: float = 0.20,
    max_multiplier: float = 1.0,
) -> StatisticalRiskAssessment:
    """Conservative sizing multiplier from volatility and tail-risk forecasts.

    The multiplier only reduces a separately calculated base size. It cannot
    increase size above 1, waive a limit, or approve an order. Callers must
    still enforce RiskLimits, freshness, liquidity, leverage and circuit
    breakers. Forecasts must be calibrated for the same symbol/horizon.
    """
    values = (
        forecast_volatility,
        reference_volatility,
        tail_probability,
        min_multiplier,
        max_multiplier,
    )
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


def assess_statistical_risk_forecast(
    forecast: Mapping[str, object],
    *,
    symbol: str,
    side: str,
    horizon: str,
    now: datetime | None = None,
    max_age_seconds: float = 30.0,
) -> StatisticalRiskAssessment:
    """Validate a scanner forecast's identity/freshness before sizing use.

    The caller must provide an explicit, timestamped forecast from the same
    symbol and timeframe. Missing forecasts are handled by the caller; a
    supplied but malformed, stale, or mismatched forecast fails closed.
    """
    if not isinstance(forecast, Mapping):
        raise ValueError("statistical forecast must be a mapping")
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be finite and positive")
    if (
        forecast.get("symbol") != symbol
        or forecast.get("side") != side
        or forecast.get("horizon") != horizon
    ):
        raise ValueError("statistical forecast symbol, side or horizon mismatch")
    if not forecast.get("model_id") or not forecast.get("model_version"):
        raise ValueError("statistical forecast model identity is required")
    try:
        sample_size = int(forecast["sample_size"])
        as_of_value = str(forecast["as_of"])
        as_of = datetime.fromisoformat(as_of_value.replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "statistical forecast sample size or timestamp is invalid"
        ) from exc
    if sample_size < 30:
        raise ValueError("statistical forecast has insufficient observations")
    try:
        predicted_up = float(forecast["predicted_up"])
        downside_probability = float(forecast["downside_probability"])
        expected_return = float(forecast["expected_return"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "statistical forecast directional metrics are invalid"
        ) from exc
    if any(
        not math.isfinite(value)
        for value in (predicted_up, downside_probability, expected_return)
    ):
        raise ValueError("statistical forecast directional metrics must be finite")
    if not 0.0 <= predicted_up <= 1.0:
        raise ValueError("predicted_up must be in [0, 1]")
    if not 0.0 <= downside_probability <= 1.0:
        raise ValueError("downside_probability must be in [0, 1]")
    if as_of.tzinfo is None:
        raise ValueError("statistical forecast timestamp must be timezone-aware")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age = (
        current.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
    ).total_seconds()
    if age < 0:
        raise ValueError("statistical forecast timestamp is in the future")
    if age > max_age_seconds:
        raise ValueError("statistical forecast is stale")
    try:
        return assess_statistical_risk(
            forecast_volatility=float(forecast["forecast_volatility"]),
            reference_volatility=float(forecast["reference_volatility"]),
            tail_probability=float(forecast["tail_probability"]),
            expected_shortfall=(
                None
                if forecast.get("expected_shortfall") is None
                else float(forecast["expected_shortfall"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"statistical forecast risk metrics are invalid: {exc}"
        ) from exc
