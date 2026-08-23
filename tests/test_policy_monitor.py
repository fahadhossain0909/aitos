from aitos.journal.policy_monitor import evaluate_policy_health


def test_policy_monitor_recommends_rollback_after_degradation():
    outcomes = [{"r_multiple": 0.1} for _ in range(30)]
    result = evaluate_policy_health(
        "v2", outcomes, baseline_avg_r=0.5, min_observations=30, max_degradation=0.20
    )
    assert result.rollback_recommended is True
    assert result.reason == "rollback_recommended"


def test_policy_monitor_waits_for_sample_size():
    result = evaluate_policy_health(
        "v2", [{"r_multiple": -1.0}], baseline_avg_r=0.5, min_observations=30
    )
    assert result.rollback_recommended is False
    assert result.reason == "insufficient_observations"
