"""Multiple-testing-aware strategy selection and forecast comparison.

All functions consume chronologically aligned, out-of-sample return or loss
series. They are diagnostics, not standalone evidence of profitability.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Iterable, Mapping, Sequence


def _series(values: Iterable[float], name: str, minimum: int = 2) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum or any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain at least {minimum} finite values")
    return result


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_ppf(probability: float) -> float:
    """Acklam rational approximation to the standard-normal quantile."""
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be in (0, 1)")
    a = (
        -39.6968302866538,
        220.946098424521,
        -275.928510446969,
        138.357751867269,
        -30.6647980661472,
        2.50662827745924,
    )
    b = (
        -54.4760987982241,
        161.585836858041,
        -155.698979859887,
        66.8013118877197,
        -13.2806815528857,
    )
    c = (
        -0.00778489400243029,
        -0.322396458041136,
        -2.40075827716184,
        -2.54973253934373,
        4.37466414146497,
        2.93816398269878,
    )
    d = (0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742)
    if probability < 0.02425:
        q = math.sqrt(-2.0 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if probability > 0.97575:
        return -_normal_ppf(1.0 - probability)
    q = probability - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


@dataclass(frozen=True)
class SharpeSignificance:
    sharpe: float
    benchmark: float
    probability: float
    standard_error: float
    observations: int


def probabilistic_sharpe_ratio(
    returns: Iterable[float], *, benchmark_sharpe: float = 0.0
) -> SharpeSignificance:
    """Bailey-López de Prado PSR using sample skew and non-excess kurtosis."""
    xs = _series(returns, "returns", 3)
    if not math.isfinite(benchmark_sharpe):
        raise ValueError("benchmark_sharpe must be finite")
    sd = pstdev(xs)
    if sd <= 1e-15:
        raise ValueError("Sharpe ratio is undefined for constant returns")
    sr = mean(xs) / sd
    centered = [x - mean(xs) for x in xs]
    m2 = sum(x * x for x in centered) / len(xs)
    skew = sum(x**3 for x in centered) / len(xs) / (m2**1.5)
    kurtosis = sum(x**4 for x in centered) / len(xs) / (m2 * m2)
    variance = (1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr) / (len(xs) - 1)
    se = math.sqrt(max(variance, 1e-15))
    return SharpeSignificance(
        sr, benchmark_sharpe, _normal_cdf((sr - benchmark_sharpe) / se), se, len(xs)
    )


def deflated_sharpe_ratio(
    returns: Iterable[float], *, trials: int, variance_of_trial_sharpes: float = 1.0
) -> SharpeSignificance:
    """PSR against the expected maximum Sharpe under multiple null trials."""
    if (
        trials < 1
        or not math.isfinite(variance_of_trial_sharpes)
        or variance_of_trial_sharpes < 0
    ):
        raise ValueError("trials must be positive and trial variance non-negative")
    if trials == 1:
        benchmark = 0.0
    else:
        gamma = 0.5772156649015329
        expected_max = (1.0 - gamma) * _normal_ppf(
            1.0 - 1.0 / trials
        ) + gamma * _normal_ppf(1.0 - 1.0 / (trials * math.e))
        benchmark = expected_max * math.sqrt(variance_of_trial_sharpes)
    return probabilistic_sharpe_ratio(returns, benchmark_sharpe=benchmark)


@dataclass(frozen=True)
class ComparisonTest:
    statistic: float
    p_value: float
    observations: int
    method: str


def diebold_mariano_test(
    loss_a: Iterable[float], loss_b: Iterable[float], *, horizon: int = 1
) -> ComparisonTest:
    """Two-sided DM test with Bartlett/Newey-West long-run variance."""
    a, b = _series(loss_a, "loss_a"), _series(loss_b, "loss_b")
    if len(a) != len(b) or horizon < 1:
        raise ValueError("loss arrays must align and horizon must be positive")
    d = [x - y for x, y in zip(a, b)]
    center = mean(d)
    n = len(d)
    lag = min(horizon - 1, n - 1)
    gamma0 = sum((x - center) ** 2 for x in d) / n
    long_run = gamma0
    for k in range(1, lag + 1):
        covariance = sum((d[t] - center) * (d[t - k] - center) for t in range(k, n)) / n
        long_run += 2.0 * (1.0 - k / (lag + 1.0)) * covariance
    if long_run <= 1e-18:
        raise ValueError("DM long-run variance is zero")
    statistic = center / math.sqrt(long_run / n)
    # Normal approximation is conservative for moderate/large samples.
    p = 2.0 * (1.0 - _normal_cdf(abs(statistic)))
    return ComparisonTest(statistic, min(1.0, max(0.0, p)), n, "diebold_mariano_hac")


@dataclass(frozen=True)
class BootstrapTest:
    statistic: float
    p_value: float
    observations: int
    resamples: int
    block_length: int


def _circular_block_sample(
    values: Sequence[float], block_length: int, rng: random.Random
) -> list[float]:
    n = len(values)
    out: list[float] = []
    while len(out) < n:
        start = rng.randrange(n)
        out.extend(values[(start + i) % n] for i in range(block_length))
    return out[:n]


def white_reality_check(
    strategy_returns: Mapping[str, Iterable[float]],
    *,
    resamples: int = 1000,
    block_length: int = 5,
    seed: int = 0,
) -> BootstrapTest:
    """White's Reality Check using centered circular-block bootstrap maxima."""
    if len(strategy_returns) < 1 or resamples < 100:
        raise ValueError("at least one strategy and 100 resamples are required")
    names = tuple(sorted(strategy_returns))
    matrix = tuple(_series(strategy_returns[name], name) for name in names)
    n = len(matrix[0])
    if any(len(row) != n for row in matrix) or not 1 <= block_length <= n:
        raise ValueError("strategy returns must align and block length be valid")
    observed = max(mean(row) for row in matrix)
    centered = tuple(tuple(x - mean(row) for x in row) for row in matrix)
    rng = random.Random(seed)
    exceed = 0
    for _ in range(resamples):
        maxima = [
            mean(_circular_block_sample(row, block_length, rng)) for row in centered
        ]
        if max(maxima) >= observed:
            exceed += 1
    return BootstrapTest(
        observed, (exceed + 1) / (resamples + 1), n, resamples, block_length
    )


@dataclass(frozen=True)
class ModelConfidenceSet:
    included: tuple[str, ...]
    excluded: tuple[str, ...]
    p_values: Mapping[str, float]
    confidence: float
    observations: int


def model_confidence_set(
    losses: Mapping[str, Iterable[float]],
    *,
    confidence: float = 0.90,
    resamples: int = 500,
    block_length: int = 5,
    seed: int = 0,
) -> ModelConfidenceSet:
    """Range-based block-bootstrap MCS elimination with step-down p-values.

    This compact implementation is intended for research screening. It returns
    the surviving set and per-elimination bootstrap p-values, not a substitute
    for a full Hansen-Lunde-Nason implementation with studentized T_max/T_R.
    """
    if len(losses) < 2 or not 0.5 < confidence < 1.0 or resamples < 100:
        raise ValueError("invalid MCS configuration")
    names = tuple(sorted(losses))
    matrix = {name: _series(losses[name], name) for name in names}
    n = len(matrix[names[0]])
    if any(len(row) != n for row in matrix.values()) or not 1 <= block_length <= n:
        raise ValueError("loss series must align and block length be valid")
    rng = random.Random(seed)
    active = list(names)
    p_values: dict[str, float] = {}
    alpha = 1.0 - confidence
    while len(active) > 1:
        means = {name: mean(matrix[name]) for name in active}
        worst = max(active, key=means.__getitem__)
        observed = means[worst] - min(means.values())
        exceed = 0
        for _ in range(resamples):
            samples = {
                name: _circular_block_sample(
                    [x - means[name] for x in matrix[name]], block_length, rng
                )
                for name in active
            }
            max_stat = max(
                mean(samples[name]) - min(mean(samples[x]) for x in active)
                for name in active
            )
            if max_stat >= observed:
                exceed += 1
        p_value = (exceed + 1) / (resamples + 1)
        p_values[worst] = p_value
        if p_value > alpha:
            break
        active.remove(worst)
    return ModelConfidenceSet(
        tuple(active),
        tuple(name for name in names if name not in active),
        p_values,
        confidence,
        n,
    )


@dataclass(frozen=True)
class AdjustedPValues:
    adjusted: Mapping[str, float]
    rejected: tuple[str, ...]
    method: str
    alpha: float


def correct_multiple_testing(
    p_values: Mapping[str, float], *, alpha: float = 0.05, method: str = "holm"
) -> AdjustedPValues:
    """Holm step-down family-wise or Benjamini-Hochberg FDR correction."""
    if not p_values or not 0.0 < alpha < 1.0:
        raise ValueError("p_values and alpha are required")
    values = {str(k): float(v) for k, v in p_values.items()}
    if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values.values()):
        raise ValueError("p-values must be finite and in [0,1]")
    ordered = sorted(values, key=values.get)
    m = len(ordered)
    adjusted: dict[str, float] = {}
    method = method.lower()
    if method == "holm":
        running = 0.0
        for i, name in enumerate(ordered):
            running = max(running, min(1.0, (m - i) * values[name]))
            adjusted[name] = running
    elif method in {"bh", "fdr_bh"}:
        running = 1.0
        for i in range(m - 1, -1, -1):
            name = ordered[i]
            running = min(running, values[name] * m / (i + 1))
            adjusted[name] = min(1.0, running)
    else:
        raise ValueError("method must be holm or bh")
    rejected = tuple(name for name in values if adjusted[name] <= alpha)
    return AdjustedPValues(adjusted, rejected, method, alpha)
