from aitos.intelligence.contextual_layers import (
    positioning_context,
    positioning_evidence,
)


def test_positioning_context_is_optional_and_serializable():
    context = positioning_context(open_interest_change=0.02, funding_rate=0.001)
    assert context.available
    payload = context.to_dict()
    assert payload["open_interest_change"] == 0.02
    assert payload["available"] is True


def test_positioning_evidence_is_context_not_binary_signal():
    context = positioning_context(
        open_interest_change=0.02,
        funding_rate=0.01,
        basis=0.01,
        source="test",
    )
    evidence = positioning_evidence(context, "long")
    assert set(evidence) >= {"open_interest_positioning", "funding_crowding", "basis_context"}
    assert all(0.0 <= value <= 10.0 for value in evidence.values())
