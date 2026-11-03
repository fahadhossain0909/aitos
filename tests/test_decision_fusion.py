import pytest

from aitos.kernel.decision_fusion import DecisionFusionEngine

FULL_SCORES = {
    "trend_strength": 9.0,
    "liquidity_quality": 8.0,
    "order_flow_bias": 9.0,
    "auction_context": 8.0,
    "volatility": 7.0,
    "market_regime": 9.0,
    "lead_lag": 7.0,
    "funding_rate": 8.0,
    "open_interest_trend": 8.0,
    "rl_confidence": 7.0,
    "footprint_interaction": 8.0,
}


@pytest.mark.parametrize("direction", ["long", "short"])
def test_strong_component_evidence_supports_direction(direction):
    result = DecisionFusionEngine().fuse(direction, FULL_SCORES)

    assert result.direction == direction
    assert result.confidence >= 0.60
    assert len(result.contributions) == len(FULL_SCORES) + 1
    assert result.missing_components == ("graph_historical_support",)


def test_weak_evidence_returns_neutral():
    result = DecisionFusionEngine().fuse(
        "long",
        {"trend_strength": 2.0, "order_flow_bias": 3.0, "liquidity_quality": 2.0},
    )

    assert result.direction == "neutral"
    assert 0.0 < result.confidence < 0.60
    assert "auction_context" in result.missing_components


def test_missing_components_are_not_treated_as_zero():
    result = DecisionFusionEngine().fuse("long", {"trend_strength": 10.0})

    assert result.confidence == 1.0
    assert result.direction == "long"
    available = [item for item in result.contributions if item.available]
    assert len(available) == 1
    assert available[0].source == "trend_strength"
    assert "graph_historical_support" in result.missing_components


def test_scores_are_clamped_to_scanner_scale():
    result = DecisionFusionEngine().fuse(
        "long", {"trend_strength": 100.0, "order_flow_bias": -20.0}
    )

    scores = {item.source: item.score for item in result.contributions}
    assert scores["trend_strength"] == 10.0
    assert scores["order_flow_bias"] == 0.0


def test_graph_historical_support_contributes_when_available():
    result = DecisionFusionEngine().fuse(
        "long",
        {"trend_strength": 8.0, "graph_historical_support": 10.0},
        {"trend_strength": True, "graph_historical_support": True},
    )

    graph = next(
        item
        for item in result.contributions
        if item.source == "graph_historical_support"
    )
    assert graph.available is True
    assert graph.weight == 0.05
    assert graph.weighted_score == 0.5
    assert "graph_historical_support" not in result.missing_components


def test_graph_unavailable_does_not_dilute_confidence():
    engine = DecisionFusionEngine()
    base = engine.fuse("long", {"trend_strength": 8.0})
    unavailable = engine.fuse(
        "long",
        {"trend_strength": 8.0, "graph_historical_support": 5.0},
        {"trend_strength": True, "graph_historical_support": False},
    )

    assert unavailable.confidence == base.confidence
    assert "graph_historical_support" in unavailable.missing_components
    graph = next(
        item
        for item in unavailable.contributions
        if item.source == "graph_historical_support"
    )
    assert graph.available is False


def test_fuse_context_returns_none_without_component_evidence():
    engine = DecisionFusionEngine()
    assert engine.fuse_context({"direction": "long"}) is None


def test_fuse_context_accepts_scanner_style_context():
    engine = DecisionFusionEngine()
    result = engine.fuse_context({"direction": "long", "component_scores": FULL_SCORES})

    assert result is not None
    assert result.direction == "long"
