"""Offline evaluation harnesses for AITOS policy comparison.

Phase F of the Market-Path / Exit-Intelligence architecture: replay price
paths under static TP/SL versus the new Exit Intelligence policy and compare
outcomes without touching live or paper trading code.
"""

from aitos.evaluation.exit_replay import (
    ExitPolicyResult,
    ExitReplayEngine,
    ExitReplaySummary,
    PriceBar,
    TradeScenario,
    compare_policies,
)

__all__ = [
    "ExitPolicyResult",
    "ExitReplayEngine",
    "ExitReplaySummary",
    "PriceBar",
    "TradeScenario",
    "compare_policies",
]
