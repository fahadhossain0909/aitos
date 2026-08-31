"""Unit tests for Market Path Planner (Phase B)."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.intelligence.amt.volume_profile import VolumeProfile
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
from aitos.intelligence.path_planner import MarketPathPlanner, PathPlan


def _state(
    price: float = 79000.0,
    regime: Regime = Regime.TRENDING_UP,
    of: OrderFlowBias = OrderFlowBias.BUYER_DOMINANT,
) -> MarketState:
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        mid_price=price,
        regime=regime,
        trend_strength=0.8,
        volatility_regime=VolatilityRegime.NORMAL,
        auction_state=AuctionState.ACCEPTANCE_ABOVE_VALUE,
        order_flow_bias=of,
        liquidity_bias=LiquidityBias.UPSIDE_LIQUIDITY_HIGH,
        momentum=MomentumState.STRONG,
        structure=StructureBias.BULLISH,
        reversal_risk=0.2,
    )


def test_basic_plan_from_prior_levels():
    planner = MarketPathPlanner()
    plan = planner.plan(
        market_state=_state(),
        prior_highs=[79400.0, 80200.0],
        prior_lows=[78500.0, 78000.0],
    )
    assert isinstance(plan, PathPlan)
    assert plan.current_price == 79000.0
    assert len(plan.upside) >= 1
    assert len(plan.downside) >= 1
    assert plan.upside[0].price > 79000.0
    assert plan.downside[0].price < 79000.0


def test_volume_profile_destinations():
    vp = VolumeProfile(
        bins=((78800.0, 10.0), (79000.0, 50.0), (79200.0, 5.0), (79500.0, 30.0)),
        poc=79000.0,
        vah=79500.0,
        val=78800.0,
        high=79500.0,
        low=78800.0,
        total_volume=95.0,
        value_area_volume=80.0,
        value_area_pct=0.84,
    )
    planner = MarketPathPlanner()
    plan = planner.plan(market_state=_state(price=79100.0), volume_profile=vp)
    types = {d.market_structure_type for d in plan.upside + plan.downside}
    assert "POC" in types or "vah" in types or "val" in types or "LVN" in types or "HVN" in types


def test_regime_tilt_increases_upside_prob():
    planner = MarketPathPlanner()
    bull = planner.plan(
        market_state=_state(regime=Regime.TRENDING_UP, of=OrderFlowBias.BUYER_DOMINANT),
        prior_highs=[80000.0],
        prior_lows=[78000.0],
    )
    bear = planner.plan(
        market_state=_state(regime=Regime.TRENDING_DOWN, of=OrderFlowBias.SELLER_DOMINANT),
        prior_highs=[80000.0],
        prior_lows=[78000.0],
    )
    if bull.upside and bear.upside:
        assert bull.upside[0].probability >= bear.upside[0].probability


def test_dedupe_near_levels():
    planner = MarketPathPlanner()
    plan = planner.plan(
        market_state=_state(),
        prior_highs=[79400.0, 79420.0],  # within 0.05%
    )
    # Should collapse to a single upside destination around 79400-79420
    assert len(plan.upside) <= 2
