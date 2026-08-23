from aitos.journal.adaptive_policy import AdaptivePolicyEngine
from aitos.journal.performance_evaluator import (PerformanceReport,
                                                 PerformanceSlice)


def report(*slices):
    return PerformanceReport(10, 10, 10, 0.0, 0.0, 0.0, 0.5, list(slices))


def test_weak_regime_is_disabled_and_tightened():
    engine = AdaptivePolicyEngine(min_trades=5)
    candidate = engine.propose(
        report(PerformanceSlice("regime", "range", 10, 3, 7, -2, -0.2, -0.2, 0.3)),
        "cand-1",
    )
    policy = candidate.regimes["range"]
    assert policy.enabled is False
    assert policy.min_confidence == 0.90


def test_strong_regime_can_lower_threshold_but_not_below_floor():
    engine = AdaptivePolicyEngine(min_trades=5)
    candidate = engine.propose(
        report(PerformanceSlice("regime", "trend", 10, 7, 3, 2, 0.2, 0.2, 0.7)),
        "cand-2",
    )
    policy = candidate.regimes["trend"]
    assert policy.enabled is True
    assert policy.min_confidence == 0.55


def test_insufficient_sample_keeps_base_policy():
    engine = AdaptivePolicyEngine(min_trades=20)
    candidate = engine.propose(
        report(PerformanceSlice("regime", "unknown", 3, 3, 0, 1, 0.3, 0.3, 1.0)),
        "cand-3",
    )
    policy = candidate.regimes["unknown"]
    assert policy.enabled is True
    assert policy.min_confidence == 0.60
