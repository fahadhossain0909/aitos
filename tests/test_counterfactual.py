from aitos.xai.counterfactual import composite_score, counterfactual_for_threshold

WEIGHTS = {
    "trend_strength": 0.15,
    "liquidity_quality": 0.10,
    "order_flow_bias": 0.15,
    "auction_context": 0.10,
    "volatility": 0.05,
    "market_regime": 0.10,
    "lead_lag": 0.10,
    "funding_rate": 0.10,
    "open_interest_trend": 0.10,
    "rl_confidence": 0.05,
}


def make_scores(**overrides):
    defaults = {k: 5.0 for k in WEIGHTS}
    defaults.update(overrides)
    return defaults


def test_composite_score_all_fives_is_fifty():
    scores = make_scores()
    assert composite_score(scores, WEIGHTS) == 50.0


def test_composite_score_reflects_weighted_dimensions():
    scores = make_scores(trend_strength=10.0)
    assert composite_score(scores, WEIGHTS) > 50.0


def test_counterfactual_passing_candidate_explains_what_would_flip_it():
    scores = make_scores(
        trend_strength=9.0, order_flow_bias=9.0
    )  # clears a 55 threshold
    current = composite_score(scores, WEIGHTS)
    assert current >= 55.0
    messages = counterfactual_for_threshold(scores, WEIGHTS, threshold=55.0)
    assert messages  # at least one dimension's removal would flip the decision
    assert any("trend strength" in m or "order flow bias" in m for m in messages)


def test_counterfactual_failing_candidate_explains_what_would_pass_it():
    scores = make_scores(trend_strength=2.0, order_flow_bias=2.0)
    current = composite_score(scores, WEIGHTS)
    assert current < 50.0
    messages = counterfactual_for_threshold(scores, WEIGHTS, threshold=50.0)
    assert messages
    assert any("improved from" in m for m in messages)


def test_counterfactual_no_single_dimension_can_bridge_a_huge_gap():
    scores = make_scores(**{k: 0.0 for k in WEIGHTS})
    messages = counterfactual_for_threshold(scores, WEIGHTS, threshold=99.0)
    assert (
        messages == []
    )  # no single dimension improving from 0 to 10 can close this gap
