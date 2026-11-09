"""Student-t copula fitting and portfolio VaR/ES backtesting diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class StudentTCopulaFit:
    symbols: tuple[str, ...]
    degrees_of_freedom: float
    correlation_matrix: tuple[tuple[float, ...], ...]
    tail_dependence_matrix: tuple[tuple[float, ...], ...]
    log_likelihood: float
    observations: int


def fit_student_t_copula(
    returns: Mapping[str, Sequence[float]],
    *,
    degrees_of_freedom_grid: tuple[float, ...] = (3, 4, 5, 7, 10, 15, 20, 30),
    shrinkage: float = 0.02,
) -> StudentTCopulaFit:
    """Fit a rank-based Student-t copula by profile pseudo-likelihood."""
    try:
        import numpy as np
        from scipy.stats import multivariate_t, rankdata, t
    except ImportError as exc:
        raise RuntimeError("Student-t copula fitting requires scipy and numpy") from exc

    symbols = tuple(sorted(returns))
    if len(symbols) < 2 or not degrees_of_freedom_grid or not 0.0 <= shrinkage < 1.0:
        raise ValueError("copula requires >=2 assets and shrinkage in [0, 1)")
    matrix = [np.asarray(returns[symbol], dtype=float) for symbol in symbols]
    n = len(matrix[0])
    if n < 50 or any(len(row) != n for row in matrix):
        raise ValueError("copula requires >=50 aligned observations per asset")
    if any(not np.isfinite(row).all() for row in matrix):
        raise ValueError("copula returns must be finite")
    if any(float(np.std(row)) <= 1e-12 for row in matrix):
        raise ValueError("copula returns must have non-zero variance")
    if any(
        not math.isfinite(float(df)) or float(df) <= 2.0
        for df in degrees_of_freedom_grid
    ):
        raise ValueError("degrees of freedom candidates must be finite and >2")

    uniforms = np.column_stack(
        [(rankdata(row, method="average") - 0.5) / n for row in matrix]
    )
    best = None
    identity = np.eye(len(symbols))
    for degrees in sorted(set(float(value) for value in degrees_of_freedom_grid)):
        scores = t.ppf(uniforms, df=degrees)
        correlation = np.corrcoef(scores, rowvar=False)
        correlation = (1.0 - shrinkage) * correlation + shrinkage * identity
        eigenvalues, eigenvectors = np.linalg.eigh(correlation)
        eigenvalues = np.maximum(eigenvalues, 1e-8)
        correlation = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
        diagonal = np.sqrt(np.diag(correlation))
        correlation = correlation / np.outer(diagonal, diagonal)
        joint = multivariate_t.logpdf(
            scores, loc=np.zeros(len(symbols)), shape=correlation, df=degrees
        )
        marginal = sum(t.logpdf(scores[:, i], df=degrees) for i in range(len(symbols)))
        log_likelihood = float(np.sum(joint - marginal))
        if best is None or log_likelihood > best[0]:
            best = (log_likelihood, degrees, correlation)
    log_likelihood, degrees, correlation = best
    tail = np.eye(len(symbols))
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            rho = float(max(-0.999999, min(0.999999, correlation[i, j])))
            dependence = 2.0 * t.cdf(
                -math.sqrt((degrees + 1.0) * (1.0 - rho) / (1.0 + rho)),
                df=degrees + 1.0,
            )
            tail[i, j] = tail[j, i] = float(dependence)
    return StudentTCopulaFit(
        symbols,
        degrees,
        tuple(tuple(float(value) for value in row) for row in correlation),
        tuple(tuple(float(value) for value in row) for row in tail),
        log_likelihood,
        n,
    )


@dataclass(frozen=True)
class VaRESBacktest:
    observations: int
    exceptions: int
    expected_exceptions: float
    exception_rate: float
    kupiec_lr: float
    kupiec_p_value: float
    independence_lr: float
    independence_p_value: float
    conditional_coverage_lr: float
    conditional_coverage_p_value: float
    realized_tail_loss: float | None
    forecast_tail_loss: float | None
    expected_shortfall_ratio: float | None


def _bernoulli_log_likelihood(
    successes: int, failures: int, probability: float
) -> float:
    value = 0.0
    if successes:
        if probability <= 0.0:
            return -math.inf
        value += successes * math.log(probability)
    if failures:
        if probability >= 1.0:
            return -math.inf
        value += failures * math.log1p(-probability)
    return value


def backtest_var_es(
    realized_returns: Iterable[float],
    var_forecasts: Iterable[float],
    es_forecasts: Iterable[float],
    *,
    confidence: float = 0.975,
) -> VaRESBacktest:
    """Kupiec coverage, Christoffersen independence and ES severity checks.

    Forecasts and realized returns must use the same horizon and currency
    return scale. VaR/ES forecasts are positive loss magnitudes; exceptions
    occur when the realized loss (-return) exceeds VaR.
    """
    returns = tuple(float(value) for value in realized_returns)
    var = tuple(float(value) for value in var_forecasts)
    es = tuple(float(value) for value in es_forecasts)
    if not 0.5 < confidence < 1.0 or len(returns) < 30:
        raise ValueError("backtest requires confidence in (0.5,1) and >=30 rows")
    if len(returns) != len(var) or len(var) != len(es):
        raise ValueError("returns, VaR and ES forecasts must align")
    if any(not math.isfinite(value) for value in returns + var + es):
        raise ValueError("backtest inputs must be finite")
    if any(value < 0 for value in var) or any(
        expected < threshold for expected, threshold in zip(es, var)
    ):
        raise ValueError("loss forecasts require ES >= VaR >= 0")
    exceptions = tuple(
        int(-value > threshold) for value, threshold in zip(returns, var)
    )
    n = len(exceptions)
    count = sum(exceptions)
    tail_probability = 1.0 - confidence
    observed_probability = count / n
    null_ll = _bernoulli_log_likelihood(count, n - count, tail_probability)
    alternative_ll = _bernoulli_log_likelihood(count, n - count, observed_probability)
    kupiec_lr = max(0.0, 2.0 * (alternative_ll - null_ll))
    kupiec_p = math.erfc(math.sqrt(kupiec_lr / 2.0))

    n00 = n01 = n10 = n11 = 0
    for previous, current in zip(exceptions, exceptions[1:]):
        if previous == 0 and current == 0:
            n00 += 1
        elif previous == 0 and current == 1:
            n01 += 1
        elif previous == 1 and current == 0:
            n10 += 1
        else:
            n11 += 1
    transitions = n - 1
    p = (n01 + n11) / transitions
    p01 = n01 / (n00 + n01) if n00 + n01 else 0.0
    p11 = n11 / (n10 + n11) if n10 + n11 else 0.0
    iid_ll = _bernoulli_log_likelihood(n01 + n11, n00 + n10, p)
    markov_ll = _bernoulli_log_likelihood(n01, n00, p01) + _bernoulli_log_likelihood(
        n11, n10, p11
    )
    independence_lr = max(0.0, 2.0 * (markov_ll - iid_ll))
    independence_p = math.erfc(math.sqrt(independence_lr / 2.0))
    conditional_lr = kupiec_lr + independence_lr
    conditional_p = math.exp(-conditional_lr / 2.0)

    tail_losses = [-returns[i] for i, hit in enumerate(exceptions) if hit]
    tail_forecasts = [es[i] for i, hit in enumerate(exceptions) if hit]
    realized_tail = sum(tail_losses) / len(tail_losses) if tail_losses else None
    forecast_tail = (
        sum(tail_forecasts) / len(tail_forecasts) if tail_forecasts else None
    )
    if tail_forecasts and forecast_tail <= 0.0:
        raise ValueError("ES forecasts must be positive when VaR exceptions occur")
    ratio = (
        realized_tail / forecast_tail
        if realized_tail is not None and forecast_tail
        else None
    )
    return VaRESBacktest(
        n,
        count,
        n * tail_probability,
        observed_probability,
        kupiec_lr,
        kupiec_p,
        independence_lr,
        independence_p,
        conditional_lr,
        conditional_p,
        realized_tail,
        forecast_tail,
        ratio,
    )
