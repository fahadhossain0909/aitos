"""Chronological out-of-sample validation for Futures/Spot lead-lag hypotheses."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Sequence


@dataclass(frozen=True, slots=True)
class LeadLagOOSResult:
    leader: str
    lag_seconds: int
    train_correlation: float
    test_correlation: float
    confidence_low: float
    confidence_high: float
    observations: int
    synchronous_test_correlation: float
    incremental_abs_correlation: float
    regime_correlations: dict[str, float]
    validated: bool


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    n = min(len(left), len(right))
    if n < 3:
        return 0.0
    x, y = left[:n], right[:n]
    mx, my = sum(x) / n, sum(y) / n
    dx, dy = [v - mx for v in x], [v - my for v in y]
    denominator = sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return 0.0 if denominator <= 1e-18 else sum(a * b for a, b in zip(dx, dy)) / denominator


def _hac_interval(values: Sequence[float], confidence_z: float = 1.96) -> tuple[float, float]:
    n = len(values)
    if n < 8:
        return -1.0, 1.0
    mean = sum(values) / n
    centered = [value - mean for value in values]
    bandwidth = min(int(n ** 0.25), 10)
    long_run_variance = sum(value * value for value in centered) / n
    for lag in range(1, bandwidth + 1):
        covariance = sum(
            centered[index] * centered[index - lag]
            for index in range(lag, n)
        ) / n
        weight = 1.0 - lag / (bandwidth + 1)
        long_run_variance += 2.0 * weight * covariance
    standard_error = sqrt(max(0.0, long_run_variance) / n)
    return max(-1.0, mean - confidence_z * standard_error), min(
        1.0, mean + confidence_z * standard_error
    )


def validate_futures_spot_lead_lag_oos(
    *,
    spot_returns: Sequence[float],
    futures_returns: Sequence[float],
    regimes: Sequence[str] | None = None,
    max_lag_seconds: int = 5,
    train_fraction: float = 0.6,
    min_train_observations: int = 60,
    min_test_observations: int = 30,
    min_abs_test_correlation: float = 0.15,
    min_incremental_abs_correlation: float = 0.03,
) -> LeadLagOOSResult:
    """Select a leader/lag only on the chronological train segment.

    Test confidence uses a Newey-West HAC interval over standardized
    cross-products, reducing false certainty from serial dependence. This is
    validation evidence, not an entry signal or production promotion action.
    """
    n = min(len(spot_returns), len(futures_returns))
    if regimes is not None and len(regimes) < n:
        raise ValueError("regime labels must cover every aligned return")
    if not 0.5 <= train_fraction < 0.9 or max_lag_seconds < 0:
        raise ValueError("invalid train fraction or maximum lag")
    split = int(n * train_fraction)
    if max_lag_seconds >= split:
        raise ValueError("maximum lag must be shorter than the training segment")
    if split < min_train_observations or n - split < min_test_observations:
        raise ValueError("insufficient chronological train/test observations")
    spot = list(map(float, spot_returns[:n]))
    futures = list(map(float, futures_returns[:n]))
    if any(not (value == value and abs(value) != float("inf")) for value in spot + futures):
        raise ValueError("returns must be finite")
    candidates: list[tuple[float, str, int]] = []
    for lag in range(max_lag_seconds + 1):
        if lag == 0:
            corr = _pearson(spot[:split], futures[:split])
            candidates.append((abs(corr), "synchronous", 0))
            continue
        # Spot leads: earlier Spot return explains later Futures return.
        candidates.append(
            (abs(_pearson(spot[: split - lag], futures[lag:split])), "spot", lag)
        )
        # Futures leads: earlier Futures return explains later Spot return.
        candidates.append(
            (abs(_pearson(futures[: split - lag], spot[lag:split])), "futures", lag)
        )
    _, leader, lag = max(candidates, key=lambda item: item[0])
    if leader == "spot":
        train_corr = _pearson(spot[: split - lag], futures[lag:split])
        x, y = spot[split - lag : n - lag], futures[split:n]
    elif leader == "futures":
        train_corr = _pearson(futures[: split - lag], spot[lag:split])
        x, y = futures[split - lag : n - lag], spot[split:n]
    else:
        train_corr = _pearson(spot[:split], futures[:split])
        x, y = spot[split:n], futures[split:n]
    test_corr = _pearson(x, y)
    sync_corr = _pearson(spot[split:n], futures[split:n])
    if len(x) != len(y) or len(x) < min_test_observations:
        raise ValueError("selected lag leaves too few out-of-sample observations")
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sqrt(sum((v - mx) ** 2 for v in x) / len(x))
    sy = sqrt(sum((v - my) ** 2 for v in y) / len(y))
    products = [
        ((a - mx) / sx) * ((b - my) / sy) if sx > 0 and sy > 0 else 0.0
        for a, b in zip(x, y)
    ]
    low, high = _hac_interval(products)
    regime_scores: dict[str, float] = {}
    if regimes is not None:
        test_regimes = list(regimes[split:n])
        for regime in sorted(set(test_regimes)):
            indices = [i for i, label in enumerate(test_regimes) if label == regime]
            if len(indices) < 8:
                continue
            if leader == "spot" and lag:
                left = [spot[split - lag + i] for i in indices]
                right = [futures[split + i] for i in indices]
            elif leader == "futures" and lag:
                left = [futures[split - lag + i] for i in indices]
                right = [spot[split + i] for i in indices]
            else:
                left = [spot[split + i] for i in indices]
                right = [futures[split + i] for i in indices]
            regime_scores[regime] = _pearson(left, right)
    incremental = abs(test_corr) - abs(sync_corr)
    validated = (
        abs(test_corr) >= min_abs_test_correlation
        and low * high > 0
        and incremental >= min_incremental_abs_correlation
    )
    return LeadLagOOSResult(
        leader=leader,
        lag_seconds=lag,
        train_correlation=train_corr,
        test_correlation=test_corr,
        confidence_low=low,
        confidence_high=high,
        observations=len(x),
        synchronous_test_correlation=sync_corr,
        incremental_abs_correlation=incremental,
        regime_correlations=regime_scores,
        validated=validated,
    )
