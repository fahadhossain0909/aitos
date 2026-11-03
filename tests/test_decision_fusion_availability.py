"""Phase B: missing features must not dilute fusion confidence as fake 5.0s."""

from aitos.kernel.decision_fusion import DecisionFusionEngine


def test_missing_components_excluded_from_denominator():
    engine = DecisionFusionEngine(
        weights={"trend_strength": 0.5, "rl_confidence": 0.5}, min_confidence=0.55
    )
    result = engine.fuse(
        "long",
        {"trend_strength": 8.0},
        component_availability={"trend_strength": True, "rl_confidence": False},
    )
    assert "rl_confidence" in result.missing_components
    assert result.confidence == 0.8
    assert result.direction == "long"


def test_neutral_placeholder_scores_can_be_marked_unavailable():
    engine = DecisionFusionEngine(
        weights={
            "trend_strength": 0.5,
            "footprint_interaction": 0.5,
        },
        min_confidence=0.60,
    )
    result = engine.fuse(
        "long",
        {"trend_strength": 7.0, "footprint_interaction": 5.0},
        component_availability={
            "trend_strength": True,
            "footprint_interaction": False,
        },
    )
    assert result.confidence == 0.7
    assert result.direction == "long"


def test_footprint_interaction_is_in_default_weights():
    engine = DecisionFusionEngine()
    assert "footprint_interaction" in engine.weights
