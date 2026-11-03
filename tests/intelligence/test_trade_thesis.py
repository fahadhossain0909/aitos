"""Tests for Trade Thesis engine."""

from datetime import datetime, timezone

from aitos.intelligence.market_state.models import (
    AuctionState,
    LiquidityBias,
    MarketState,
    MomentumState,
    OrderFlowBias,
    Regime,
    StructureBias,
    VolatilityRegime,
)
from aitos.intelligence.trade_thesis import TradeThesisEngine
from aitos.intelligence.trade_thesis.models import ThesisHealth


def _state(
    *,
    structure=StructureBias.BULLISH,
    of=OrderFlowBias.BUYER_DOMINANT,
    momentum=MomentumState.STRONG,
    reversal=0.2,
):
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        mid_price=100.0,
        regime=Regime.TRENDING_UP,
        trend_strength=0.7,
        volatility_regime=VolatilityRegime.NORMAL,
        auction_state=AuctionState.ACCEPTANCE_ABOVE_VALUE,
        order_flow_bias=of,
        liquidity_bias=LiquidityBias.BALANCED,
        momentum=momentum,
        structure=structure,
        reversal_risk=reversal,
    )


def test_build_thesis_captures_structure_and_of():
    engine = TradeThesisEngine()
    thesis = engine.build_from_entry(
        trade_id="t1",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        market_state=_state(),
        structural_invalidation_price=98.5,
        expected_path_prices=(101.0, 103.0),
    )
    codes = {c.code for c in thesis.components}
    assert "bullish_structure" in codes
    assert "buyer_imbalance" in codes
    assert thesis.invalidation_price == 98.5


def test_evaluate_intact_when_market_aligned():
    engine = TradeThesisEngine()
    state = _state()
    thesis = engine.build_from_entry(
        trade_id="t1",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        market_state=state,
        structural_invalidation_price=98.0,
    )
    ev = engine.evaluate(thesis, state, current_price=101.0)
    assert ev.health == ThesisHealth.INTACT
    assert ev.consistency_score >= 0.9


def test_evaluate_invalidated_on_structure_break_price():
    engine = TradeThesisEngine()
    state = _state()
    thesis = engine.build_from_entry(
        trade_id="t1",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        market_state=state,
        structural_invalidation_price=98.0,
    )
    ev = engine.evaluate(thesis, state, current_price=97.5)
    assert ev.health == ThesisHealth.INVALIDATED
    assert "structure_break" in ev.breached_invalidations
