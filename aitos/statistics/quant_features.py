"""Dependency-light quantitative statistical features for AITOS.

These estimators are diagnostic/forecast primitives, not trade signals. Inputs
must be chronological, finite, and expressed in decimal returns unless stated.
No function in this module submits orders or overrides hard risk limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Iterable, Sequence


_EPS = 1e-12


def _clean(values: Iterable[float], *, minimum: int = 1) -> tuple[float, ...]:
    result = tuple(float(x) for x in values)
    if len(result) < minimum or any(not math.isfinite(x) for x in result):
        raise ValueError(f"expected at least {minimum} finite observations")
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class DistributionFeatures:
    count: int
    mean: float
    volatility: float
    downside_semivolatility: float
    skewness: float
    excess_kurtosis: float
    q05: float
    median: float
    q95: float
    signal_to_noise: float


@dataclass(frozen=True)
class VolatilityForecast:
    short: float
    medium: float
    long: float
    forecast: float
    downside: float
    jump_ratio: float


@dataclass(frozen=True)
class ChangePointDiagnostic:
    score: float
    detected: bool
    positive_cusum: float
    negative_cusum: float


@dataclass(frozen=True)
class HawkesIntensity:
    buy: float
    sell: float
    buy_excitation: float
    sell_excitation: float
    imbalance: float


@dataclass(frozen=True)
class StatisticalDiagnostics:
    distribution: DistributionFeatures
    volatility: VolatilityForecast
    change_point: ChangePointDiagnostic
    permutation_entropy: float
    hurst_exponent: float
    hawkes: HawkesIntensity


def _quantile(xs: Sequence[float], q: float) -> float:
    ordered = sorted(xs)
    position = (len(ordered) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    weight = position - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def distribution_features(returns: Iterable[float]) -> DistributionFeatures:
    xs = _clean(returns, minimum=3)
    mu = mean(xs)
    sigma = pstdev(xs)
    centered = [x - mu for x in xs]
    if sigma > _EPS:
        skew = mean((x / sigma) ** 3 for x in centered)
        kurt = mean((x / sigma) ** 4 for x in centered) - 3.0
    else:
        skew = kurt = 0.0
    downside = math.sqrt(mean(min(0.0, x) ** 2 for x in xs))
    return DistributionFeatures(
        count=len(xs),
        mean=mu,
        volatility=sigma,
        downside_semivolatility=downside,
        skewness=skew,
        excess_kurtosis=kurt,
        q05=_quantile(xs, 0.05),
        median=_quantile(xs, 0.5),
        q95=_quantile(xs, 0.95),
        signal_to_noise=mu / max(sigma, _EPS),
    )


def har_rv_forecast(
    returns: Iterable[float],
    *,
    short_window: int = 5,
    medium_window: int = 22,
    long_window: int = 66,
) -> VolatilityForecast:
    """HAR-style multi-horizon realized-volatility baseline.

    The forecast is a transparent equal-weight blend of available horizon RVs,
    not a fitted Corsi regression. Calibrate coefficients out of sample before
    using this as a production forecast.
    """
    xs = _clean(returns, minimum=3)
    if min(short_window, medium_window, long_window) <= 0:
        raise ValueError("windows must be positive")

    def rv(window: int) -> float:
        sample = xs[-min(window, len(xs)) :]
        return math.sqrt(mean(x * x for x in sample))

    short, medium, long = rv(short_window), rv(medium_window), rv(long_window)
    forecast = (short + medium + long) / 3.0
    downside = math.sqrt(mean(min(0.0, x) ** 2 for x in xs))
    baseline = max(mean(x * x for x in xs), _EPS)
    # Large one-step returns relative to the sample's second moment are a
    # descriptive jump proxy, not a formal bipower-variation jump test.
    jump_ratio = _clamp(
        max((x * x for x in xs), default=0.0) / (len(xs) * baseline), 0.0, 1.0
    )
    return VolatilityForecast(short, medium, long, forecast, downside, jump_ratio)


def cusum_change_point(
    values: Iterable[float], *, threshold: float = 5.0, drift: float = 0.5
) -> ChangePointDiagnostic:
    """Two-sided standardized CUSUM diagnostic on a finite observation window."""
    xs = _clean(values, minimum=5)
    if threshold <= 0 or drift < 0:
        raise ValueError("threshold must be positive and drift non-negative")
    mu = mean(xs)
    sigma = max(pstdev(xs), _EPS)
    pos = neg = 0.0
    for value in xs:
        z = (value - mu) / sigma
        pos = max(0.0, pos + z - drift)
        neg = max(0.0, neg - z - drift)
    score = max(pos, neg)
    return ChangePointDiagnostic(score, score >= threshold, pos, neg)


def permutation_entropy(
    values: Iterable[float], *, order: int = 3, delay: int = 1
) -> float:
    """Normalized Bandt-Pompe permutation entropy in [0, 1]."""
    xs = _clean(values, minimum=order * delay + 1)
    if order < 2 or delay < 1:
        raise ValueError("order must be >= 2 and delay >= 1")
    patterns: dict[tuple[int, ...], int] = {}
    for start in range(len(xs) - (order - 1) * delay):
        window = xs[start : start + order * delay : delay]
        # Stable index tie-break makes tied observations deterministic.
        pattern = tuple(sorted(range(order), key=lambda i: (window[i], i)))
        patterns[pattern] = patterns.get(pattern, 0) + 1
    total = sum(patterns.values())
    probabilities = [count / total for count in patterns.values()]
    entropy = -sum(p * math.log(p) for p in probabilities if p > 0)
    maximum = math.lgamma(order + 1)
    return _clamp(entropy / maximum, 0.0, 1.0)


def hurst_exponent(
    values: Iterable[float], *, min_lag: int = 2, max_lag: int = 20
) -> float:
    """Coarse log(RMS-difference)-log(lag) slope diagnostic.

    This is a screening estimate, not a statistically calibrated Hurst test.
    Use multiple windows and confidence intervals before interpretation.
    """
    xs = _clean(values, minimum=max_lag + 3)
    if min_lag < 2 or max_lag <= min_lag:
        raise ValueError("invalid lag range")
    points = []
    for lag in range(min_lag, min(max_lag, len(xs) // 2) + 1):
        diffs = [xs[i + lag] - xs[i] for i in range(len(xs) - lag)]
        scale = math.sqrt(mean(d * d for d in diffs))
        if scale > _EPS:
            points.append((math.log(lag), math.log(scale)))
    if len(points) < 2:
        return 0.5
    xbar = mean(x for x, _ in points)
    ybar = mean(y for _, y in points)
    denominator = sum((x - xbar) ** 2 for x, _ in points)
    slope = sum((x - xbar) * (y - ybar) for x, y in points) / max(denominator, _EPS)
    return _clamp(slope, 0.0, 1.0)


def exponential_hawkes_intensity(
    event_signs: Iterable[int], *, decay: float = 0.2, baseline: float = 0.1
) -> HawkesIntensity:
    """Discrete event-time exponential excitation proxy.

    event_signs must be +1 (buy), -1 (sell), or 0 (no classified event).
    This estimates excitation scores from event order; it is not a fitted
    continuous-time Hawkes maximum-likelihood estimator.
    """
    signs = tuple(int(x) for x in event_signs)
    if not signs or any(x not in (-1, 0, 1) for x in signs):
        raise ValueError("event_signs must be a non-empty sequence of -1, 0, +1")
    if not 0 < decay <= 1 or baseline < 0:
        raise ValueError("decay must be in (0, 1] and baseline non-negative")
    buy_exc = sell_exc = 0.0
    buy_count = sell_count = 0
    for sign in signs:
        buy_exc *= 1.0 - decay
        sell_exc *= 1.0 - decay
        if sign > 0:
            buy_count += 1
            buy_exc += decay
        elif sign < 0:
            sell_count += 1
            sell_exc += decay
    buy = baseline + buy_count / len(signs) + buy_exc
    sell = baseline + sell_count / len(signs) + sell_exc
    return HawkesIntensity(
        buy,
        sell,
        buy_exc,
        sell_exc,
        (buy - sell) / max(buy + sell, _EPS),
    )


def kalman_beta(
    y: Iterable[float],
    x: Iterable[float],
    *,
    observation_variance: float = 1e-4,
    process_variance: float = 1e-6,
) -> float:
    """Scalar random-walk Kalman estimate of a time-varying hedge beta."""
    ys, xs = _clean(y, minimum=3), _clean(x, minimum=3)
    if len(ys) != len(xs):
        raise ValueError("y and x must have equal lengths")
    if observation_variance <= 0 or process_variance < 0:
        raise ValueError("invalid Kalman variances")
    beta, variance = 0.0, 1.0
    for target, factor in zip(ys, xs):
        variance += process_variance
        innovation_variance = factor * factor * variance + observation_variance
        gain = variance * factor / max(innovation_variance, _EPS)
        beta += gain * (target - factor * beta)
        variance = max(_EPS, (1.0 - gain * factor) * variance)
    return beta


def diagnose(
    returns: Iterable[float], event_signs: Iterable[int] = ()
) -> StatisticalDiagnostics:
    """Build a consistent diagnostic snapshot from one chronological window."""
    xs = _clean(returns, minimum=66)
    signs = tuple(event_signs) or tuple(0 for _ in xs)
    return StatisticalDiagnostics(
        distribution=distribution_features(xs),
        volatility=har_rv_forecast(xs),
        change_point=cusum_change_point(xs),
        permutation_entropy=permutation_entropy(xs),
        hurst_exponent=hurst_exponent(xs),
        hawkes=exponential_hawkes_intensity(signs),
    )
