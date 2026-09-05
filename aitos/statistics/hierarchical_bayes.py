"""Hierarchical Normal-Normal Bayesian model for contract-level returns."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .models import BayesianPosterior


class HierarchicalBayes:
    """Shrink a contract posterior toward a global market prior.

    The caller may supply global observations from the same asset class/market universe.
    This prevents thin-history contracts from behaving as if they had independent full
    statistical certainty.
    """

    def __init__(
        self,
        prior_mean: float = 0.0,
        prior_variance: float = 0.0001,
        prior_strength: float = 20.0,
    ) -> None:
        self.prior_mean = prior_mean
        self.prior_variance = max(prior_variance, 1e-12)
        self.prior_strength = max(prior_strength, 1.0)

    def fit(
        self, contract_returns: Sequence[float], global_returns: Sequence[float] = ()
    ) -> BayesianPosterior:
        xs = [float(x) for x in contract_returns if math.isfinite(float(x))]
        gs = [float(x) for x in global_returns if math.isfinite(float(x))]
        if gs:
            gm = sum(gs) / len(gs)
            gv = max(sum((x - gm) ** 2 for x in gs) / max(1, len(gs) - 1), 1e-12)
        else:
            gm, gv = self.prior_mean, self.prior_variance
        if not xs:
            cm, cv = gm, gv
        else:
            cm0 = sum(xs) / len(xs)
            obs_var = max(sum((x - cm0) ** 2 for x in xs) / max(1, len(xs) - 1), 1e-12)
            prior_var = max(gv / self.prior_strength, 1e-12)
            likelihood_precision = len(xs) / obs_var
            posterior_precision = 1.0 / prior_var + likelihood_precision
            cv = 1.0 / posterior_precision
            cm = cv * (gm / prior_var + cm0 * likelihood_precision)
        predictive_var = max(cv + gv, 1e-12)
        z = cm / math.sqrt(predictive_var)
        p_positive = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return BayesianPosterior(
            gm,
            gv,
            cm,
            cv,
            p_positive,
            min(1.0, len(xs) / (len(xs) + self.prior_strength)),
        )
