"""Offline/shadow evaluation for policy candidates.

The evaluator compares a candidate regime gate with the baseline on historical
outcomes. It does not execute trades and does not mutate the live policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from aitos.journal.adaptive_policy import PolicyCandidate
from aitos.journal.performance_evaluator import PerformanceReport


@dataclass(frozen=True)
class ShadowPolicyResult:
    candidate_id: str
    baseline_trades: int
    candidate_trades: int
    baseline_pnl: float
    candidate_pnl: float
    baseline_average_r: float
    candidate_average_r: float
    baseline_win_rate: float
    candidate_win_rate: float
    pnl_delta: float
    average_r_delta: float
    win_rate_delta: float
    eligible_for_promotion: bool

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def evaluate_shadow(
    report: PerformanceReport, candidate: PolicyCandidate
) -> ShadowPolicyResult:
    """Estimate candidate performance from regime-level historical slices.

    A candidate only removes regimes explicitly disabled. For enabled regimes,
    historical outcomes are retained; the confidence threshold is represented
    as governance metadata until decision snapshots carry confidence at trade
    outcome granularity. Promotion therefore requires an explicit external
    validation step and never happens automatically here.
    """
    regime_map = candidate.regimes
    selected = [
        s
        for s in report.slices
        if s.key == "regime"
        and regime_map.get(s.value, None) is not None
        and regime_map[s.value].enabled
    ]
    candidate_trades = sum(s.trades for s in selected)
    candidate_pnl = sum(s.total_pnl for s in selected)
    candidate_r = (
        (sum(s.average_r_multiple * s.trades for s in selected) / candidate_trades)
        if candidate_trades
        else 0.0
    )
    candidate_wins = sum(s.wins for s in selected)
    candidate_win_rate = candidate_wins / candidate_trades if candidate_trades else 0.0

    improved = (
        candidate_pnl > report.total_pnl
        and candidate_r >= report.average_r_multiple
        and candidate_win_rate >= report.win_rate
    )
    return ShadowPolicyResult(
        candidate_id=candidate.candidate_id,
        baseline_trades=report.outcome_count,
        candidate_trades=candidate_trades,
        baseline_pnl=report.total_pnl,
        candidate_pnl=candidate_pnl,
        baseline_average_r=report.average_r_multiple,
        candidate_average_r=candidate_r,
        baseline_win_rate=report.win_rate,
        candidate_win_rate=candidate_win_rate,
        pnl_delta=candidate_pnl - report.total_pnl,
        average_r_delta=candidate_r - report.average_r_multiple,
        win_rate_delta=candidate_win_rate - report.win_rate,
        eligible_for_promotion=improved and candidate_trades >= candidate.min_trades,
    )
