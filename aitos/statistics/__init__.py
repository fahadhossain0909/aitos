"""Predictive statistical intelligence for AITOS.

A-Stat is deliberately market-agnostic and dependency-light. It produces
probabilities, regimes, volatility, tail risk and strategy context without
owning execution decisions.
"""

from .engine import AStatEngine
from .evt import POTGPD
from .garch import GARCH11
from .hierarchical_bayes import HierarchicalBayes
from .hmm import MarkovSwitchingModel
from .models import (
    AStatObservation,
    AStatResult,
    BayesianEvidence,
    BayesianPosterior,
    DirectionProbability,
    EVTTail,
    GARCHForecast,
    HMMState,
    RegimeProbability,
    StatisticalModelResult,
    StrategyStatContext,
)
from .router import StatisticalStrategyRouter, StrategyScore, strategy_contexts
from .stack import ContractStatisticalStack
from .quant_features import (
    ChangePointDiagnostic,
    DistributionFeatures,
    HawkesIntensity,
    StatisticalDiagnostics,
    VolatilityForecast,
    cusum_change_point,
    diagnose,
    distribution_features,
    exponential_hawkes_intensity,
    har_rv_forecast,
    hurst_exponent,
    kalman_beta,
    permutation_entropy,
)
from .risk_overlay import StatisticalRiskAssessment, assess_statistical_risk
from .validation import brier_score, log_loss, purged_walk_forward_folds, qlike_loss

__all__ = [
    "GARCH11",
    "ChangePointDiagnostic",
    "DistributionFeatures",
    "HawkesIntensity",
    "StatisticalDiagnostics",
    "StatisticalRiskAssessment",
    "VolatilityForecast",
    "assess_statistical_risk",
    "brier_score",
    "cusum_change_point",
    "diagnose",
    "distribution_features",
    "exponential_hawkes_intensity",
    "har_rv_forecast",
    "hurst_exponent",
    "kalman_beta",
    "log_loss",
    "permutation_entropy",
    "purged_walk_forward_folds",
    "qlike_loss",
    "POTGPD",
    "AStatEngine",
    "AStatObservation",
    "AStatResult",
    "BayesianEvidence",
    "BayesianPosterior",
    "ContractStatisticalStack",
    "DirectionProbability",
    "EVTTail",
    "GARCHForecast",
    "HMMState",
    "HierarchicalBayes",
    "MarkovSwitchingModel",
    "RegimeProbability",
    "StatisticalModelResult",
    "StatisticalStrategyRouter",
    "StrategyScore",
    "StrategyStatContext",
    "strategy_contexts",
]
