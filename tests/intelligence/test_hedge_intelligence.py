from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.intelligence.exit_intelligence.models import ExitAction
from aitos.intelligence.hedge_intelligence import HedgeAction, HedgeIntelligenceEngine
from aitos.intelligence.trade_thesis.models import ThesisEvaluation, ThesisHealth


def _state(
    *,
    structure="BULLISH",
    of="SELLER_DOMINANT",
    momentum="WEAK",
    vol="EXPANDING",
    liq="DOWNSIDE_LIQUIDITY_HIGH",
    reversal=1.0
):
    return SimpleNamespace(
        structure=SimpleNamespace(value=structure),
        order_flow_bias=SimpleNamespace(value=of),
        momentum=SimpleNamespace(value=momentum),
        volatility_regime=SimpleNamespace(value=vol),
        liquidity_bias=SimpleNamespace(value=liq),
        reversal_risk=reversal,
    )


def _thesis(health=ThesisHealth.INTACT):
    return ThesisEvaluation(health, 1.0, (), (), ())


def test_opens_partial_hedge_only_when_risk_is_high():
    engine = HedgeIntelligenceEngine({"enabled": True})
    decision = engine.evaluate(
        symbol="BTCUSDT",
        primary_side="LONG",
        market_state=_state(),
        thesis_eval=_thesis(),
        exit_action=ExitAction.MANAGE,
        current_price=95.0,
        primary_entry_price=100.0,
        primary_r_distance=5.0,
    )
    assert decision.action is HedgeAction.OPEN
    assert decision.hedge_side == "SHORT"
    assert 0.25 <= decision.hedge_ratio <= 0.50


def test_closes_hedge_on_primary_direction_recovery():
    engine = HedgeIntelligenceEngine({"enabled": True})
    decision = engine.evaluate(
        symbol="BTCUSDT",
        primary_side="LONG",
        market_state=_state(
            structure="BULLISH",
            of="BUYER_DOMINANT",
            momentum="STRONG",
            vol="NORMAL",
            liq="UPSIDE_LIQUIDITY_HIGH",
        ),
        thesis_eval=_thesis(),
        exit_action=ExitAction.HOLD,
        current_price=100.0,
        primary_entry_price=100.0,
        primary_r_distance=5.0,
        hedge_active=True,
        hedge_opened_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    assert decision.action is HedgeAction.CLOSE
    assert decision.hedge_side == "SHORT"


def test_primary_exit_has_priority_over_hedge():
    engine = HedgeIntelligenceEngine({"enabled": True})
    decision = engine.evaluate(
        symbol="BTCUSDT",
        primary_side="LONG",
        market_state=_state(),
        thesis_eval=_thesis(ThesisHealth.INVALIDATED),
        exit_action=ExitAction.EXIT,
        current_price=95.0,
        primary_entry_price=100.0,
        primary_r_distance=5.0,
        hedge_active=True,
    )
    assert decision.action is HedgeAction.CLOSE


def test_disabled_by_default_is_non_intrusive():
    engine = HedgeIntelligenceEngine()
    decision = engine.evaluate(
        symbol="BTCUSDT",
        primary_side="LONG",
        market_state=_state(),
        thesis_eval=_thesis(),
        exit_action=ExitAction.HOLD,
        current_price=95.0,
        primary_entry_price=100.0,
        primary_r_distance=5.0,
    )
    assert decision.action is HedgeAction.NONE
