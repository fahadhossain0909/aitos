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
    DirectionProbability,
    RegimeProbability,
    StrategyStatContext,
)
from .router import StatisticalStrategyRouter, StrategyScore, strategy_contexts
from .stack import ContractStatisticalStack

__all__ = [
    "AStatEngine",
    "AStatObservation",
    "AStatResult",
    "BayesianEvidence",
    "DirectionProbability",
    "RegimeProbability",
    "StrategyStatContext",
    "StatisticalStrategyRouter",
    "StrategyScore",
    "strategy_contexts",
    "MarkovSwitchingModel",
    "GARCH11",
    "POTGPD",
    "HierarchicalBayes",
    "ContractStatisticalStack",
]
