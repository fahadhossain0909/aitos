"""Multivariate volatility dependence and portfolio tail-risk diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Mapping, Sequence

from .garch import GARCH11


@dataclass(frozen=True)
class DCCForecast:
    symbols: tuple[str, ...]
    conditional_volatilities: tuple[float, ...]
    correlation_matrix: tuple[tuple[float, ...], ...]
    alpha: float
    beta: float
    persistence: float
    observations: int
    gaussian_copula_correlation: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class PortfolioTailRisk:
    expected_shortfall: float
    value_at_risk: float
    confidence: float
    observations: int
    portfolio_returns: tuple[float, ...]


def _corr(x, y):
    mx, my = mean(x), mean(y)
    dx, dy = [a - mx for a in x], [b - my for b in y]
    denom = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    return 0.0 if denom <= 1e-15 else sum(a * b for a, b in zip(dx, dy)) / denom


def _inverse_positive_definite(matrix):
    """Invert a small positive-definite matrix with pivoted elimination."""
    n = len(matrix)
    augmented = [
        [float(matrix[i][j]) for j in range(n)]
        + [1.0 if i == j else 0.0 for j in range(n)]
        for i in range(n)
    ]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-12:
            raise ArithmeticError("DCC correlation matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [row[n:] for row in augmented]


def _logdet_positive_definite(matrix):
    """Stable log determinant via Cholesky decomposition."""
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if value <= 1e-12:
                    raise ArithmeticError(
                        "DCC correlation matrix is not positive definite"
                    )
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value / lower[j][j]
    return 2.0 * sum(math.log(lower[i][i]) for i in range(n))


def _normal_score_ranks(values):
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    scores = [0.0] * len(values)
    n = len(values)

    # Acklam inverse-normal approximation, using midpoint empirical ranks.
    def inv(p):
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
        if p < 0.02425:
            q = math.sqrt(-2 * math.log(p))
            return (
                ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
            ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        if p > 0.97575:
            return -inv(1 - p)
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )

    for rank, index in enumerate(ordered, 1):
        scores[index] = inv((rank - 0.5) / n)
    return scores


class DCCGARCH:
    """Two-stage GARCH + DCC(1,1) Gaussian-copula dependence estimator."""

    def fit_forecast(self, returns: Mapping[str, Sequence[float]]) -> DCCForecast:
        symbols = tuple(sorted(returns))
        if len(symbols) < 2:
            raise ValueError("DCC requires at least two assets")
        series = [tuple(float(v) for v in returns[s]) for s in symbols]
        n = len(series[0])
        if n < 50 or any(len(x) != n for x in series):
            raise ValueError("DCC requires >=50 aligned observations per asset")
        if any(not math.isfinite(v) for row in series for v in row):
            raise ValueError("DCC returns must be finite")
        standardized = []
        vol_forecasts = []
        for xs in series:
            fit = GARCH11().fit_forecast(xs)
            variance = max(fit.long_run_variance, 1e-12)
            mu = mean(xs)
            z = []
            for value in xs:
                variance = max(
                    fit.omega + fit.alpha * (value - mu) ** 2 + fit.beta * variance,
                    1e-12,
                )
                z.append((value - mu) / math.sqrt(variance))
            standardized.append(z)
            vol_forecasts.append(math.sqrt(variance))
        qbar = [
            [
                sum(row[i] * row[j] for row in zip(*standardized)) / n
                for j in range(len(symbols))
            ]
            for i in range(len(symbols))
        ]
        best = None
        for alpha in (0.01, 0.03, 0.05, 0.08, 0.12, 0.16):
            for beta in (0.70, 0.80, 0.88, 0.92, 0.95, 0.97):
                if alpha + beta >= 0.995:
                    continue
                q = [row[:] for row in qbar]
                loss = 0.0
                for t in range(n):
                    z = [standardized[k][t] for k in range(len(symbols))]
                    diag = [math.sqrt(max(q[i][i], 1e-12)) for i in range(len(symbols))]
                    corr = [
                        [q[i][j] / (diag[i] * diag[j]) for j in range(len(symbols))]
                        for i in range(len(symbols))
                    ]
                    try:
                        inverse_corr = _inverse_positive_definite(corr)
                        loss += _logdet_positive_definite(corr) + sum(
                            z[i]
                            * sum(
                                inverse_corr[i][j] * z[j] for j in range(len(symbols))
                            )
                            for i in range(len(symbols))
                        )
                    except ArithmeticError:
                        loss = float("inf")
                        break
                    q = [
                        [
                            (1 - alpha - beta) * qbar[i][j]
                            + alpha * z[i] * z[j]
                            + beta * q[i][j]
                            for j in range(len(symbols))
                        ]
                        for i in range(len(symbols))
                    ]
                if best is None or loss < best[0]:
                    best = (loss, alpha, beta)
        if best is None or not math.isfinite(best[0]):
            raise ValueError("DCC correlation dynamics could not be estimated")
        _, alpha, beta = best
        q = [row[:] for row in qbar]
        for t in range(n):
            z = [standardized[k][t] for k in range(len(symbols))]
            q = [
                [
                    (1 - alpha - beta) * qbar[i][j]
                    + alpha * z[i] * z[j]
                    + beta * q[i][j]
                    for j in range(len(symbols))
                ]
                for i in range(len(symbols))
            ]
        diag = [math.sqrt(max(q[i][i], 1e-12)) for i in range(len(symbols))]
        corr = tuple(
            tuple(q[i][j] / (diag[i] * diag[j]) for j in range(len(symbols)))
            for i in range(len(symbols))
        )
        rank_scores = [_normal_score_ranks(xs) for xs in series]
        copula = tuple(
            tuple(_corr(rank_scores[i], rank_scores[j]) for j in range(len(symbols)))
            for i in range(len(symbols))
        )
        return DCCForecast(
            symbols, tuple(vol_forecasts), corr, alpha, beta, alpha + beta, n, copula
        )


def aggregate_portfolio_tail_risk(
    returns: Mapping[str, Sequence[float]],
    weights: Mapping[str, float],
    *,
    confidence: float = 0.975,
) -> PortfolioTailRisk:
    """Historical-simulation VaR/ES on aligned, weighted portfolio returns."""
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be in (0.5, 1)")
    symbols = tuple(sorted(weights))
    if not symbols or set(symbols) != set(returns):
        raise ValueError("weights and return series must have identical symbols")
    if any(not math.isfinite(float(weights[s])) for s in symbols):
        raise ValueError("weights must be finite")
    n = len(returns[symbols[0]])
    if n < 20 or any(len(returns[s]) != n for s in symbols):
        raise ValueError("aligned portfolio returns require >=20 observations")
    if any(not math.isfinite(float(v)) for s in symbols for v in returns[s]):
        raise ValueError("returns must be finite")
    portfolio = tuple(
        sum(float(weights[s]) * float(returns[s][i]) for s in symbols) for i in range(n)
    )
    losses = sorted(-value for value in portfolio)
    index = min(n - 1, max(0, math.ceil(confidence * n) - 1))
    var = losses[index]
    tail = losses[index:]
    es = sum(tail) / len(tail)
    return PortfolioTailRisk(es, var, confidence, n, portfolio)
