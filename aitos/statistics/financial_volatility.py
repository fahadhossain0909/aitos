"""Lightweight stochastic, jump-diffusion and rough-volatility estimators.

These are causal diagnostics for research features, not full calibrated
Heston/Bates/rough-Bergomi pricing engines. They are deterministic and use
only the supplied historical return window.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class StochasticVolatilityEstimate:
    log_variance: float
    variance: float
    persistence: float
    long_run_variance: float
    innovation_volatility: float
    observations: int


@dataclass(frozen=True)
class JumpDiffusionEstimate:
    diffusion_variance: float
    jump_intensity_per_observation: float
    jump_mean: float
    jump_volatility: float
    jump_variance_fraction: float
    jump_count: int
    observations: int


@dataclass(frozen=True)
class RoughVolatilityEstimate:
    hurst_exponent: float
    rough: bool
    scales_used: int
    observations: int


def _returns(values: Iterable[float], *, minimum: int = 16) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum:
        raise ValueError(f"at least {minimum} returns are required")
    if any(not math.isfinite(value) for value in result):
        raise ValueError("returns must be finite")
    return result


def estimate_stochastic_volatility(
    returns: Iterable[float], *, persistence_cap: float = 0.995
) -> StochasticVolatilityEstimate:
    """Fit an AR(1) log-squared-return proxy and filter its terminal state.

    The observation equation uses log(r² + epsilon); the AR(1) fit estimates
    latent log variance persistence and process innovation. This is a robust
    lightweight SV proxy, not a full maximum-likelihood Heston fit.
    """
    values = _returns(returns)
    observations = tuple(math.log(value * value + 1e-12) for value in values)
    mean_y = sum(observations) / len(observations)
    previous = observations[:-1]
    current = observations[1:]
    mean_previous = sum(previous) / len(previous)
    mean_current = sum(current) / len(current)
    denominator = sum((value - mean_previous) ** 2 for value in previous)
    persistence = (
        sum((x - mean_previous) * (y - mean_current) for x, y in zip(previous, current))
        / denominator
        if denominator > 1e-15
        else 0.0
    )
    persistence = max(-0.99, min(persistence_cap, persistence))
    intercept = mean_current - persistence * mean_previous
    long_run_log_variance = intercept / max(1e-6, 1.0 - persistence)
    residuals = tuple(
        y - intercept - persistence * x for x, y in zip(previous, current)
    )
    innovation_variance = sum(value * value for value in residuals) / max(
        1, len(residuals) - 2
    )
    # One-step filtering is causal; the last observed log-square is shrunk
    # toward the fitted latent long-run state.
    filtered_log_variance = (
        persistence * observations[-1] + (1.0 - persistence) * long_run_log_variance
    )
    variance = max(1e-12, math.exp(min(50.0, filtered_log_variance)))
    long_run_variance = max(1e-12, math.exp(min(50.0, long_run_log_variance)))
    return StochasticVolatilityEstimate(
        log_variance=filtered_log_variance,
        variance=variance,
        persistence=persistence,
        long_run_variance=long_run_variance,
        innovation_volatility=math.sqrt(max(0.0, innovation_variance)),
        observations=len(values),
    )


def estimate_jump_diffusion(
    returns: Iterable[float], *, jump_sigma: float = 4.0
) -> JumpDiffusionEstimate:
    """Separate robustly detected return jumps from the diffusion component.

    Jump threshold is based on the median absolute deviation of the sample.
    Intensity is jumps per observation; convert to calendar intensity only
    when the observation cadence is known.
    """
    values = _returns(returns)
    if not math.isfinite(jump_sigma) or jump_sigma <= 0:
        raise ValueError("jump_sigma must be positive")
    center = median(values)
    mad = median(abs(value - center) for value in values)
    robust_sigma = max(1.4826 * mad, 1e-8)
    jumps = tuple(
        value for value in values if abs(value - center) > jump_sigma * robust_sigma
    )
    diffusion = tuple(
        value for value in values if abs(value - center) <= jump_sigma * robust_sigma
    )
    diffusion_mean = sum(diffusion) / len(diffusion) if diffusion else 0.0
    diffusion_variance = (
        sum((value - diffusion_mean) ** 2 for value in diffusion)
        / max(1, len(diffusion) - 1)
        if diffusion
        else 0.0
    )
    jump_mean = sum(jumps) / len(jumps) if jumps else 0.0
    jump_variance = (
        sum((value - jump_mean) ** 2 for value in jumps) / max(1, len(jumps) - 1)
        if len(jumps) > 1
        else 0.0
    )
    jump_component = (len(jumps) / len(values)) * (
        jump_variance + jump_mean * jump_mean
    )
    total = diffusion_variance + jump_component
    return JumpDiffusionEstimate(
        diffusion_variance=diffusion_variance,
        jump_intensity_per_observation=len(jumps) / len(values),
        jump_mean=jump_mean,
        jump_volatility=math.sqrt(max(0.0, jump_variance)),
        jump_variance_fraction=jump_component / total if total > 0 else 0.0,
        jump_count=len(jumps),
        observations=len(values),
    )


def estimate_rough_volatility(
    returns: Iterable[float], *, max_scale: int = 16
) -> RoughVolatilityEstimate:
    """Estimate a roughness/Hurst proxy from aggregated-return variograms.

    H is half the log-log slope of variance of k-step aggregated returns.
    Values below 0.5 indicate rougher-than-Brownian scaling; this is a
    diagnostic proxy, not a full rough-volatility model calibration.
    """
    values = _returns(returns, minimum=max(32, max_scale * 2))
    if max_scale < 2:
        raise ValueError("max_scale must be at least 2")
    scales = [scale for scale in (1, 2, 4, 8, 16, 32) if scale <= max_scale]
    points = []
    for scale in scales:
        aggregates = [
            sum(values[start : start + scale])
            for start in range(0, len(values) - scale + 1, scale)
        ]
        if len(aggregates) < 4:
            continue
        average = sum(aggregates) / len(aggregates)
        variance = sum((value - average) ** 2 for value in aggregates) / (
            len(aggregates) - 1
        )
        if variance > 1e-18:
            points.append((math.log(scale), math.log(variance)))
    if len(points) < 2:
        raise ValueError("insufficient non-degenerate aggregation scales")
    x_mean = sum(point[0] for point in points) / len(points)
    y_mean = sum(point[1] for point in points) / len(points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    hurst = max(0.01, min(0.99, slope / 2.0))
    return RoughVolatilityEstimate(
        hurst_exponent=hurst,
        rough=hurst < 0.5,
        scales_used=len(points),
        observations=len(values),
    )


def financial_volatility_features(returns: Iterable[float]) -> dict[str, float]:
    """Return stable scalar features for the Financial ML candidate schema."""
    values = tuple(float(value) for value in returns)
    if len(values) < 32 or any(not math.isfinite(value) for value in values):
        return {
            "sv_log_variance": 0.0,
            "sv_persistence": 0.0,
            "jump_intensity": 0.0,
            "jump_variance_fraction": 0.0,
            "rough_hurst": 0.5,
        }
    sv = estimate_stochastic_volatility(values)
    jump = estimate_jump_diffusion(values)
    try:
        rough = estimate_rough_volatility(values)
        hurst = rough.hurst_exponent
    except ValueError:
        hurst = 0.5
    return {
        "sv_log_variance": sv.log_variance,
        "sv_persistence": sv.persistence,
        "jump_intensity": jump.jump_intensity_per_observation,
        "jump_variance_fraction": jump.jump_variance_fraction,
        "rough_hurst": hurst,
    }
