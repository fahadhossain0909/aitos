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

__all__ = [
    "GARCH11",
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
