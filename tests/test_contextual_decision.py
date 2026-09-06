from aitos.intelligence.contextual_decision import ContextualDecisionEngine

BASE = {
    "trend_strength": 8.0,
    "liquidity_quality": 8.0,
    "order_flow_bias": 8.5,
    "auction_context": 7.5,
    "volatility": 6.0,
    "market_regime": 8.0,
}


def test_strong_consensus_produces_directional_scenario():
    result = ContextualDecisionEngine().build(
        direction="long",
        component_scores=BASE,
        context={"regime": "trending"},
    )
    assert result.action == "long"
    assert result.scenarios
    assert result.scenarios[0].direction == "long"
    assert result.confidence >= 0.60


def test_mixed_evidence_is_explicitly_reported():
    scores = dict(BASE, order_flow_bias=2.0, lead_lag=2.0)
    result = ContextualDecisionEngine().build(
        direction="long", component_scores=scores, context={"regime": "ranging"}
    )
    assert result.contradictions
    assert result.invalidations


def test_extreme_volatility_is_a_risk_context_not_automatic_contradiction():
    result = ContextualDecisionEngine().build(
        direction="long",
        component_scores=BASE,
        context={"regime": "volatile", "volatility_regime": "extreme"},
    )
    assert result.market_state == "volatile:extreme"
    assert not any("extreme volatility" in x for x in result.contradictions)


def test_no_direction_is_safe_no_trade():
    result = ContextualDecisionEngine().build(
        direction="neutral", component_scores=BASE
    )
    assert result.action == "no_trade"
    assert result.confidence == 0.0
