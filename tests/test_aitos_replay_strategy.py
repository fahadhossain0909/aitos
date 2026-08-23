from aitos.backtest.aitos_strategy import AITOSReplayStrategy


def test_replay_strategy_uses_decision_fusion():
    strategy = AITOSReplayStrategy()
    decision = strategy.decide(
        {
            "direction": "long",
            "component_scores": {
                "trend_strength": 9,
                "liquidity_quality": 8,
                "order_flow_bias": 9,
                "auction_context": 8,
                "volatility": 7,
                "market_regime": 8,
                "lead_lag": 7,
                "funding_rate": 6,
                "open_interest_trend": 8,
                "rl_confidence": 7,
            },
        }
    )
    assert decision is not None
    assert decision.direction == "long"
    assert decision.authorized is True
    assert decision.confidence >= 0.60


def test_replay_strategy_returns_none_without_context():
    assert AITOSReplayStrategy().decide({}) is None
