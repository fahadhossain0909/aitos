from aitos.journal.evidence_attribution import DecisionEvidenceAttributor


def test_attributes_component_outcomes():
    records = [
        {
            "record_type": "DECISION",
            "decision_id": "d1",
            "evidence_contributions": [
                {"source": "order_flow_bias", "score": 8.0},
                {"source": "liquidity_quality", "score": 3.0},
            ],
        },
        {
            "record_type": "OUTCOME",
            "decision_id": "d1",
            "pnl": 100.0,
            "r_multiple": 1.5,
        },
    ]
    result = DecisionEvidenceAttributor.attribute(records)
    assert [item.source for item in result] == ["liquidity_quality", "order_flow_bias"]
    assert all(item.observations == 1 for item in result)
    assert all(item.outcome_win_rate == 1.0 for item in result)


def test_falls_back_to_component_scores():
    records = [
        {
            "record_type": "DECISION",
            "decision_id": "d1",
            "component_scores": {"amt": 7.0},
        },
        {
            "record_type": "OUTCOME",
            "decision_id": "d1",
            "pnl": -20.0,
            "r_multiple": -0.5,
        },
    ]
    result = DecisionEvidenceAttributor.attribute(records)
    assert result[0].source == "amt"
    assert result[0].negative_outcomes == 1


def test_ignores_outcomes_without_pnl():
    records = [
        {
            "record_type": "DECISION",
            "decision_id": "d1",
            "component_scores": {"amt": 7.0},
        },
        {"record_type": "OUTCOME", "decision_id": "d1", "r_multiple": 1.0},
    ]
    assert DecisionEvidenceAttributor.attribute(records) == []
