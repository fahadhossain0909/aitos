from aitos.journal.adaptive_policy import AdaptivePolicyEngine
from aitos.journal.performance_evaluator import (PerformanceReport,
                                                 PerformanceSlice)
from aitos.journal.shadow_policy import evaluate_shadow


def test_shadow_evaluation_does_not_promote_weak_candidate():
    report = PerformanceReport(
        decision_count=30,
        outcome_count=30,
        linked_trade_count=30,
        total_pnl=3.0,
        average_pnl=0.1,
        average_r_multiple=0.1,
        win_rate=0.6,
        slices=[
            PerformanceSlice("regime", "trend", 20, 14, 6, 4.0, 0.2, 0.2, 0.7),
            PerformanceSlice("regime", "range", 10, 4, 6, -1.0, -0.1, -0.1, 0.4),
        ],
    )
    candidate = AdaptivePolicyEngine(min_trades=10).propose(report, "cand-shadow")
    result = evaluate_shadow(report, candidate)
    assert result.candidate_id == "cand-shadow"
    # Insufficiently sampled regimes stay enabled at the baseline policy;
    # therefore the shadow set includes both historical regime slices.
    assert result.candidate_trades == 30
    assert result.candidate_pnl == 3.0
    assert result.eligible_for_promotion is False
