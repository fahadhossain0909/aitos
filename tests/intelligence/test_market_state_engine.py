"""Unit tests for Phase-A Market State Engine."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.intelligence.market_state import (
    AuctionState,
    LiquidityBias,
    MarketStateEngine,
    MomentumState,
    OrderFlowBias,
    Regime,
    StructureBias,
    VolatilityRegime,
)
from aitos.intelligence.order_flow_engine import OrderFlowFeatures


def _of(
    imbalance: float = 0.0,
    delta: float = 0.0,
    cvd: float = 0.0,
    aggression: float = 0.5,
    buy_ratio: float = 0.5,
) -> OrderFlowFeatures:
    return OrderFlowFeatures(
        trade_count=100,
        buy_volume=50.0,
        sell_volume=50.0,
        delta=delta,
        cvd=cvd,
        buy_ratio=buy_ratio,
        aggression=aggression,
        imbalance=imbalance,
        vwap=79000.0,
        last_price=79000.0,
        direction="neutral",
        timestamp=datetime.now(timezone.utc),
    )


def test_neutral_when_no_signals():
    eng = MarketStateEngine()
    state = eng.compute(symbol="BTCUSDT", mid_price=79000.0)
    assert state.regime in (Regime.RANGE, Regime.TRANSITION)
    assert state.order_flow_bias == OrderFlowBias.NEUTRAL
    assert state.auction_state == AuctionState.UNKNOWN
    assert 0.0 <= state.reversal_risk <= 1.0
    assert state.mid_price == 79000.0


def test_buyer_dominant_order_flow():
    eng = MarketStateEngine()
    of = _of(
        imbalance=8.0, aggression=0.8, buy_ratio=0.75
    )  # strong buy bias on 0-10 scale
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=79000.0,
        order_flow=of,
        trend_strength=0.8,
    )
    assert state.order_flow_bias == OrderFlowBias.BUYER_DOMINANT
    assert state.regime == Regime.TRENDING_UP
    assert state.structure == StructureBias.BULLISH
    assert "of_normalised" in state.features


def test_seller_dominant_order_flow():
    eng = MarketStateEngine()
    of = _of(imbalance=1.5, aggression=0.75, buy_ratio=0.2)
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=79000.0,
        order_flow=of,
        trend_strength=0.75,
    )
    assert state.order_flow_bias == OrderFlowBias.SELLER_DOMINANT
    assert state.regime in (Regime.TRENDING_DOWN, Regime.TRANSITION)


def test_auction_above_value():
    eng = MarketStateEngine()
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=80200.0,
        volume_profile_poc=79500.0,
        value_area_high=80000.0,
        value_area_low=79000.0,
    )
    assert state.auction_state == AuctionState.ACCEPTANCE_ABOVE_VALUE
    assert state.features["vp_vah"] == 80000.0


def test_auction_inside_value():
    eng = MarketStateEngine()
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=79500.0,
        volume_profile_poc=79400.0,
        value_area_high=80000.0,
        value_area_low=79000.0,
    )
    assert state.auction_state == AuctionState.ACCEPTANCE_INSIDE_VALUE


def test_liquidity_upside_bias():
    eng = MarketStateEngine()
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=79000.0,
        liquidity_upside_score=0.8,
        liquidity_downside_score=0.2,
    )
    assert state.liquidity_bias == LiquidityBias.UPSIDE_LIQUIDITY_HIGH


def test_reversal_risk_elevated_on_exhaustion():
    eng = MarketStateEngine()
    of = _of(imbalance=5.0, aggression=0.15)  # exhausted
    state = eng.compute(
        symbol="BTCUSDT",
        mid_price=79000.0,
        order_flow=of,
        trend_strength=0.2,
        atr_pct=3.5,  # expanding vol
        structure_bias_hint="BROKEN",
    )
    assert state.momentum == MomentumState.EXHAUSTED
    assert state.volatility_regime == VolatilityRegime.EXPANDING
    assert state.structure == StructureBias.BROKEN
    assert state.reversal_risk >= 0.6


def test_to_dict_roundtrip_keys():
    eng = MarketStateEngine()
    state = eng.compute(symbol="ETHUSDT", mid_price=3500.0, trend_strength=0.9)
    d = state.to_dict()
    assert d["symbol"] == "ETHUSDT"
    assert d["regime"] in {r.value for r in Regime}
    assert isinstance(d["features"], dict)
    assert isinstance(d["notes"], list)


def test_deterministic_same_inputs():
    eng = MarketStateEngine()
    kwargs = dict(
        symbol="BTCUSDT",
        mid_price=79000.0,
        order_flow=_of(imbalance=7.0, aggression=0.7),
        trend_strength=0.72,
        atr_pct=1.4,
        liquidity_upside_score=0.6,
        liquidity_downside_score=0.3,
        timestamp=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    a = eng.compute(**kwargs)
    b = eng.compute(**kwargs)
    assert a == b
    assert a.features == b.features
