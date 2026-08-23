from aitos.journal.evidence_shadow import evaluate_weight_candidate


def test_candidate_weight_shadow_evaluation():
    records = [
        {
            "record_type": "DECISION",
            "decision_id": "d1",
            "direction": "long",
            "evidence_contributions": {"order_flow": 1.0, "amt": 0.2},
        },
        {"record_type": "OUTCOME", "decision_id": "d1", "r_multiple": 1.0},
        {
            "record_type": "DECISION",
            "decision_id": "d2",
            "direction": "long",
            "evidence_contributions": {"order_flow": 1.0, "amt": -0.2},
        },
        {"record_type": "OUTCOME", "decision_id": "d2", "r_multiple": 0.5},
    ]
    result = evaluate_weight_candidate(
        records,
        {"order_flow": 0.5, "amt": 0.5},
        {"order_flow": 0.8, "amt": 0.2},
        min_observations=2,
    )
    assert result.observations == 2
    assert result.candidate_score >= result.baseline_score
    assert result.eligible is True


def test_insufficient_shadow_data_is_not_eligible():
    result = evaluate_weight_candidate(
        [], {"amt": 1.0}, {"amt": 1.0}, min_observations=1
    )
    assert result.eligible is False
    assert result.reason == "insufficient_observations"
