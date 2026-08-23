from aitos.risk.models import RiskAction, RiskScoreBreakdown
from aitos.xai.explanation import build_trade_explanation


def make_trade_dict(**overrides):
    defaults = dict(
        trade_id="trade-abc123",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        sl_price=98.0,
        take_profit_levels=[102.0, 104.0, 106.0],
        leverage=5.0,
        position_size_usd=1000.0,
        risk_amount_usd=100.0,
        strategy_id="opportunity-scanner",
        explanation="trend strength=8.0/10; order flow bias=7.5/10",
        agent_consensus={
            "trend_strength": 8.0,
            "order_flow_bias": 7.5,
            "liquidity_quality": 3.0,
            "funding_rate": 5.0,
        },
    )
    defaults.update(overrides)
    return defaults


def test_build_trade_explanation_basic_fields():
    explanation = build_trade_explanation(make_trade_dict())
    assert "LONG BTCUSDT" in explanation.why_trade
    assert "5.0x" in explanation.why_leverage
    assert "98.0" in explanation.why_sl
    assert "102.0" in explanation.why_tp


def test_build_trade_explanation_supporting_and_conflicting_evidence():
    explanation = build_trade_explanation(make_trade_dict())
    assert any("trend strength" in e for e in explanation.supporting_evidence)
    assert any("order flow bias" in e for e in explanation.supporting_evidence)
    assert any("liquidity quality" in e for e in explanation.conflicting_evidence)


def test_build_trade_explanation_confidence_score_averages_component_scores():
    explanation = build_trade_explanation(make_trade_dict())
    # (8.0+7.5+3.0+5.0)/4 = 5.875, /10 = 0.5875
    assert explanation.confidence_score == 0.5875


def test_build_trade_explanation_risk_assessment_adds_risk_context():
    risk_assessment = RiskScoreBreakdown(
        position_risk=20,
        market_risk=30,
        system_risk=10,
        portfolio_risk=25,
        total=45.0,
        action=RiskAction.NORMAL,
        explanation=["drawdown within limits"],
    )
    explanation = build_trade_explanation(
        make_trade_dict(), risk_assessment=risk_assessment
    )
    assert any("45.0" in r for r in explanation.risks)
    assert "drawdown within limits" in explanation.risks


def test_build_trade_explanation_handles_missing_take_profit_levels():
    explanation = build_trade_explanation(make_trade_dict(take_profit_levels=[]))
    assert "No take-profit levels" in explanation.why_tp


def test_trade_explanation_as_narrative_includes_all_sections():
    explanation = build_trade_explanation(make_trade_dict())
    narrative = explanation.as_narrative()
    assert "Trade:" in narrative
    assert "Confidence:" in narrative
    assert "Supporting:" in narrative
    assert "Conflicting:" in narrative


def test_trade_explanation_to_dict_roundtrips_keys():
    explanation = build_trade_explanation(make_trade_dict())
    d = explanation.to_dict()
    assert set(d.keys()) == {
        "why_trade",
        "why_now",
        "why_leverage",
        "why_sl",
        "why_tp",
        "confidence_score",
        "supporting_evidence",
        "conflicting_evidence",
        "risks",
        "agent_contributions",
        "market_context",
        "regime_context",
    }
