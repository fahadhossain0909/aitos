"""GARCH(1,1) conditional-volatility estimator without scipy dependency."""
from __future__ import annotations

import math
from collections.abc import Sequence

from .models import GARCHForecast


class GARCH11:
    """Quasi-likelihood GARCH(1,1) fitted by a small deterministic grid search."""

    def __init__(self, max_iter: int = 5) -> None:
        self.max_iter = max_iter

    def fit_forecast(self, returns: Sequence[float]) -> GARCHForecast:
        xs = [float(x) for x in returns if math.isfinite(float(x))]
        if len(xs) < 5:
            var = max(sum(x * x for x in xs) / max(1, len(xs)), 1e-10)
            return GARCHForecast(var, math.sqrt(var), var, 0.08, 0.90, var * 0.02, 0.98)
        mu = sum(xs) / len(xs)
        xs = [x - mu for x in xs]
        sample_var = max(sum(x * x for x in xs) / len(xs), 1e-10)
        best = (float("inf"), 0.08, 0.90)
        # Stable coarse search; enough for an online architectural baseline.
        for alpha in (0.03, 0.05, 0.08, 0.12, 0.18, 0.25):
            for beta in (0.60, 0.70, 0.80, 0.88, 0.92, 0.96):
                if alpha + beta >= 0.995:
                    continue
                omega = sample_var * (1.0 - alpha - beta)
                h = sample_var
                nll = 0.0
                for x in xs:
                    h = max(omega + alpha * (x * x) + beta * h, 1e-12)
                    nll += math.log(h) + (x * x) / h
                if nll < best[0]:
                    best = (nll, alpha, beta)
        _, alpha, beta = best
        omega = sample_var * (1.0 - alpha - beta)
        h = sample_var
        for x in xs:
            h = max(omega + alpha * x * x + beta * h, 1e-12)
        persistence = alpha + beta
        long_run = omega / max(1.0 - persistence, 1e-6)
        return GARCHForecast(h, math.sqrt(h), long_run, alpha, beta, omega, persistence)
