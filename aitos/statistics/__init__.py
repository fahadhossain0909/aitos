"""Predictive statistical intelligence for AITOS.

A-Stat is deliberately market-agnostic and dependency-light.  It produces
calibrated probabilities, regime estimates, volatility, tail risk and
strategy-specific expectancy without owning execution decisions.
"""

from .engine import AStatEngine
from .models import (
    AStatObservation,
    AStatResult,
    BayesianEvidence,
    DirectionProbability,
    RegimeProbability,
    StrategyStatContext,
)

__all__ = [
    "AStatEngine",
    "AStatObservation",
    "AStatResult",
    "BayesianEvidence",
    "DirectionProbability",
    "RegimeProbability",
    "StrategyStatContext",
]
