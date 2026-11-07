"""Advanced, deterministic statistical estimators for AITOS.

The implementations are deliberately dependency-light. They expose forecasts
and diagnostics only; callers must validate them out of sample before using
them in risk or execution decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Sequence


_EPS = 1e-12


def _finite_series(values: Iterable[float], minimum: int = 1) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum or any(not math.isfinite(value) for value in result):
        raise ValueError(f"expected at least {minimum} finite observations")
    return result


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense system using pivoted Gaussian elimination."""
    n = len(vector)
    a = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) <= _EPS:
            raise ValueError("singular regression design")
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= scale
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


@dataclass(frozen=True)
class HARForecast:
    variance: float
    volatility: float
    coefficients: tuple[float, float, float, float]
    observations: int
    fitted: bool
    r_squared: float


def fit_har_rv_forecast(
    returns: Iterable[float],
    *,
    short_window: int = 5,
    medium_window: int = 22,
    long_window: int = 66,
    ridge: float = 1e-10,
) -> HARForecast:
    """Fit a one-step HAR-RV regression to squared-return realized variance.

    Each training row uses only trailing realized-variance features and the
    following observation's squared return as target. The final prediction
    uses the latest completed feature window. This is a lightweight proxy for
    intraday realized variance; it is not a substitute for high-frequency RV.
    """
    xs = _finite_series(returns, minimum=3)
    if (
        min(short_window, medium_window, long_window) <= 0
        or not math.isfinite(ridge)
        or ridge < 0
    ):
        raise ValueError("windows must be positive and ridge non-negative")
    if short_window > medium_window or medium_window > long_window:
        raise ValueError("HAR windows must be ordered short <= medium <= long")
    squares = [value * value for value in xs]

    def features(end: int) -> tuple[float, float, float]:
        return tuple(
            mean(squares[max(0, end - window + 1) : end + 1])
            for window in (short_window, medium_window, long_window)
        )

    start = long_window - 1
    rows = [(1.0, *features(i)) for i in range(start, len(xs) - 1)]
    targets = squares[start + 1 :]
    if len(rows) < 8:
        fallback = mean(squares[-min(long_window, len(squares)) :])
        return HARForecast(
            fallback,
            math.sqrt(max(0.0, fallback)),
            (0.0, 1 / 3, 1 / 3, 1 / 3),
            len(rows),
            False,
            0.0,
        )

    # Cyclic non-negative least squares keeps RV coefficients admissible and
    # avoids unstable unconstrained normal-equation inversion.
    coefficients = [0.0, 0.0, 0.0, 0.0]
    column_norms = [
        sum(row[index] * row[index] for row in rows) + (ridge if index else 0.0)
        for index in range(4)
    ]
    for _ in range(2000):
        largest_change = 0.0
        for index in range(4):
            numerator = sum(
                row[index]
                * (
                    target
                    - sum(coefficients[j] * row[j] for j in range(4) if j != index)
                )
                for row, target in zip(rows, targets)
            )
            updated = max(0.0, numerator / max(column_norms[index], _EPS))
            largest_change = max(largest_change, abs(updated - coefficients[index]))
            coefficients[index] = updated
        if largest_change < 1e-12:
            break

    predicted = [sum(c * x for c, x in zip(coefficients, row)) for row in rows]
    target_mean = mean(targets)
    residual = sum((y - p) ** 2 for y, p in zip(targets, predicted))
    total = sum((y - target_mean) ** 2 for y in targets)
    r_squared = 1.0 - residual / total if total > _EPS else 0.0
    next_variance = max(
        0.0,
        sum(c * x for c, x in zip(coefficients, (1.0, *features(len(xs) - 1)))),
    )
    return HARForecast(
        next_variance,
        math.sqrt(next_variance),
        tuple(coefficients),
        len(rows),
        True,
        r_squared,
    )


@dataclass(frozen=True)
class BOCPDResult:
    change_point_probability: float
    expected_run_length: float
    run_length_probabilities: tuple[float, ...]
    observations: int


def _student_t_density(
    value: float, mean_value: float, scale: float, df: float
) -> float:
    scale = max(scale, 1e-10)
    z = (value - mean_value) / scale
    log_density = (
        math.lgamma((df + 1.0) / 2.0)
        - math.lgamma(df / 2.0)
        - 0.5 * math.log(df * math.pi)
        - math.log(scale)
        - ((df + 1.0) / 2.0) * math.log1p((z * z) / df)
    )
    return math.exp(max(-745.0, min(100.0, log_density)))


def bocpd_change_point(
    values: Iterable[float],
    *,
    hazard: float = 1 / 100,
    max_run_length: int = 256,
    prior_mean: float = 0.0,
    prior_kappa: float = 1e-3,
    prior_alpha: float = 1.0,
    prior_beta: float = 1e-6,
) -> BOCPDResult:
    """Bayesian online change-point detection with a Student-t predictive.

    Uses a Normal-Inverse-Gamma prior and constant hazard. The posterior is
    truncated at max_run_length for bounded runtime/memory. This is an online
    change-point probability, not a fitted multi-regime duration model.
    """
    xs = _finite_series(values, minimum=1)
    if not 0.0 < hazard < 1.0 or max_run_length < 2:
        raise ValueError("hazard must be in (0, 1) and max_run_length >= 2")
    if any(
        not math.isfinite(value) or value <= 0
        for value in (prior_kappa, prior_alpha, prior_beta)
    ):
        raise ValueError("Normal-Inverse-Gamma prior parameters must be positive")
    if not math.isfinite(prior_mean):
        raise ValueError("prior_mean must be finite")

    # One Normal-Inverse-Gamma posterior per possible run length.
    states = [(1.0, prior_mean, prior_kappa, prior_alpha, prior_beta)]
    cp_probability = 0.0
    for value in xs:
        growth: list[tuple[float, float, float, float, float]] = []
        for probability, mu, kappa, alpha, beta in states:
            df = 2.0 * alpha
            scale = math.sqrt(beta * (kappa + 1.0) / (alpha * kappa))
            predictive = _student_t_density(value, mu, scale, df)
            growth.append(
                (
                    probability * predictive * (1.0 - hazard),
                    (kappa * mu + value) / (kappa + 1.0),
                    kappa + 1.0,
                    alpha + 0.5,
                    beta + (kappa * (value - mu) ** 2) / (2.0 * (kappa + 1.0)),
                )
            )
        prior_predictive = _student_t_density(
            value,
            prior_mean,
            math.sqrt(prior_beta * (prior_kappa + 1.0) / (prior_alpha * prior_kappa)),
            2.0 * prior_alpha,
        )
        new_states = [
            (
                prior_predictive * hazard,
                prior_mean,
                prior_kappa,
                prior_alpha,
                prior_beta,
            )
        ] + growth[: max_run_length - 1]
        total = sum(item[0] for item in new_states)
        if total <= _EPS or not math.isfinite(total):
            raise ArithmeticError("BOCPD posterior normalization failed")
        states = [(p / total, mu, k, a, b) for p, mu, k, a, b in new_states]
        cp_probability = states[0][0]
    return BOCPDResult(
        change_point_probability=cp_probability,
        expected_run_length=sum(i * item[0] for i, item in enumerate(states)),
        run_length_probabilities=tuple(item[0] for item in states),
        observations=len(xs),
    )


@dataclass(frozen=True)
class GJRGARCHForecast:
    variance: float
    volatility: float
    long_run_variance: float
    omega: float
    alpha: float
    gamma: float
    beta: float
    persistence: float


class GJRGARCH11:
    """Asymmetric GJR-GARCH(1,1) Gaussian QML with constrained refinement."""

    def fit_forecast(self, returns: Sequence[float]) -> GJRGARCHForecast:
        xs = _finite_series(returns, minimum=30)
        centered = [value - mean(xs) for value in xs]
        sample_var = max(mean(value * value for value in centered), 1e-12)

        def evaluate(alpha: float, gamma: float, beta: float):
            persistence = alpha + beta + 0.5 * gamma
            if alpha < 0.0 or gamma < 0.0 or beta < 0.0 or persistence >= 0.995:
                return float("inf"), sample_var, persistence
            omega = sample_var * (1.0 - persistence)
            variance = sample_var
            loss = 0.0
            for value in centered:
                loss += math.log(variance) + value * value / variance
                variance = max(
                    omega
                    + alpha * value * value
                    + gamma * value * value * (value < 0.0)
                    + beta * variance,
                    1e-12,
                )
            return loss, variance, persistence

        candidates = [
            (alpha, gamma, beta)
            for alpha in (0.02, 0.05, 0.10, 0.20, 0.35)
            for gamma in (0.0, 0.05, 0.10, 0.20, 0.35)
            for beta in (0.50, 0.70, 0.82, 0.90, 0.95, 0.97)
            if alpha + beta + 0.5 * gamma < 0.995
        ]
        loss, params = min(
            ((evaluate(*params)[0], params) for params in candidates),
            key=lambda item: item[0],
        )
        alpha, gamma, beta = params
        for step in (0.05, 0.02, 0.005, 0.001):
            improved = True
            while improved:
                improved = False
                for index, direction in (
                    (0, -1),
                    (0, 1),
                    (1, -1),
                    (1, 1),
                    (2, -1),
                    (2, 1),
                ):
                    candidate = [alpha, gamma, beta]
                    candidate[index] += direction * step
                    candidate = [
                        max(0.0, min(0.98, candidate[0])),
                        max(0.0, min(0.98, candidate[1])),
                        max(0.0, min(0.994, candidate[2])),
                    ]
                    candidate_loss = evaluate(*candidate)[0]
                    if candidate_loss + 1e-10 < loss:
                        alpha, gamma, beta = candidate
                        loss = candidate_loss
                        improved = True
                        break
        loss, variance, persistence = evaluate(alpha, gamma, beta)
        omega = sample_var * (1.0 - persistence)
        long_run = omega / max(1.0 - persistence, 1e-8)
        return GJRGARCHForecast(
            variance,
            math.sqrt(variance),
            long_run,
            omega,
            alpha,
            gamma,
            beta,
            persistence,
        )


@dataclass(frozen=True)
class EGARCHForecast:
    variance: float
    volatility: float
    long_run_variance: float
    omega: float
    alpha: float
    gamma: float
    beta: float
    observations: int
    negative_log_likelihood: float


class EGARCH11:
    """EGARCH(1,1) Gaussian QML with constrained deterministic refinement."""

    _E_ABS_STANDARD_NORMAL = math.sqrt(2.0 / math.pi)

    def __init__(self, *, max_refinements: int = 80) -> None:
        if max_refinements <= 0:
            raise ValueError("max_refinements must be positive")
        self.max_refinements = max_refinements

    @staticmethod
    def _evaluate(
        centered: Sequence[float], alpha: float, gamma: float, beta: float
    ) -> tuple[float, float, float]:
        sample_variance = max(mean(value * value for value in centered), _EPS)
        if not 0.0 <= alpha <= 1.0 or abs(gamma) > 1.0 or abs(beta) >= 0.995:
            return float("inf"), sample_variance, math.log(sample_variance)
        omega = (1.0 - beta) * math.log(sample_variance)
        log_variance = math.log(sample_variance)
        loss = 0.0
        for value in centered:
            log_variance = max(-40.0, min(40.0, log_variance))
            variance = math.exp(log_variance)
            loss += log_variance + value * value / variance
            z = value / math.sqrt(variance)
            log_variance = (
                omega
                + beta * log_variance
                + alpha * (abs(z) - EGARCH11._E_ABS_STANDARD_NORMAL)
                + gamma * z
            )
        return loss, math.exp(max(-40.0, min(40.0, log_variance))), omega

    def fit_forecast(self, returns: Iterable[float]) -> EGARCHForecast:
        xs = _finite_series(returns, minimum=30)
        mu = mean(xs)
        centered = tuple(value - mu for value in xs)
        candidates = [
            (alpha, gamma, beta)
            for alpha in (0.05, 0.10, 0.20, 0.35, 0.50)
            for gamma in (-0.30, -0.15, 0.0, 0.15, 0.30)
            for beta in (0.50, 0.70, 0.85, 0.92, 0.97)
        ]
        best = min(
            ((self._evaluate(centered, *params)[0], params) for params in candidates),
            key=lambda item: item[0],
        )
        loss, (alpha, gamma, beta) = best
        steps = [(0.05, 0.05, 0.02), (0.02, 0.02, 0.01), (0.005, 0.005, 0.002)]
        refinements = 0
        for step in steps:
            improved = True
            while improved and refinements < self.max_refinements:
                improved = False
                for index, direction in (
                    (0, -1),
                    (0, 1),
                    (1, -1),
                    (1, 1),
                    (2, -1),
                    (2, 1),
                ):
                    values = [alpha, gamma, beta]
                    values[index] += direction * step[index]
                    candidate = (
                        max(0.0, min(1.0, values[0])),
                        max(-1.0, min(1.0, values[1])),
                        max(-0.994, min(0.994, values[2])),
                    )
                    candidate_loss = self._evaluate(centered, *candidate)[0]
                    refinements += 1
                    if candidate_loss + 1e-12 < loss:
                        alpha, gamma, beta = candidate
                        loss = candidate_loss
                        improved = True
                        break
                    if refinements >= self.max_refinements:
                        break
        loss, variance, omega = self._evaluate(centered, alpha, gamma, beta)
        long_run = math.exp(max(-40.0, min(40.0, omega / max(1.0 - beta, 1e-8))))
        return EGARCHForecast(
            variance,
            math.sqrt(variance),
            long_run,
            omega,
            alpha,
            gamma,
            beta,
            len(xs),
            loss,
        )
