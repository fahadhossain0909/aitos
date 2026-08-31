"""Exit confirmation / hysteresis tests."""

from datetime import datetime, timezone

from aitos.intelligence.exit_intelligence import ExitAction, ExitIntelligenceEngine
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
from aitos.intelligence.path_planner.models import PathPlan


def _state(
    structure=StructureBias.RANGE,
    of=OrderFlowBias.NEUTRAL,
    momentum=MomentumState.MODERATING,
    rr=0.3,
):
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        mid_price=100.0,
        regime=Regime.RANGE,
        trend_strength=0.4,
        volatility_regime=VolatilityRegime.NORMAL,
        auction_state=AuctionState.BALANCED,
        order_flow_bias=of,
        liquidity_bias=LiquidityBias.BALANCED,
        momentum=momentum,
        structure=structure,
        reversal_risk=rr,
    )


def test_hard_structure_break_exits_without_waiting():
    eie = ExitIntelligenceEngine(config={"exit_confirm_ticks": 3})
    plan = PathPlan(
        symbol="BTCUSDT",
        current_price=100.0,
        upside=(),
        downside=(),
        as_of=datetime.now(timezone.utc),
    )
    d = eie.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        current_price=99.0,
        market_state=_state(
            structure=StructureBias.BROKEN,
            of=OrderFlowBias.SELLER_DOMINANT,
            rr=0.7,
        ),
        path_plan=plan,
    )
    assert d.action == ExitAction.EXIT


def test_soft_pressure_tracks_streak():
    eie = ExitIntelligenceEngine(config={"exit_confirm_ticks": 2})
    plan = PathPlan(
        symbol="BTCUSDT",
        current_price=100.0,
        upside=(),
        downside=(),
        as_of=datetime.now(timezone.utc),
    )
    state = _state(
        structure=StructureBias.BEARISH,
        of=OrderFlowBias.SELLER_DOMINANT,
        momentum=MomentumState.WEAK,
        rr=0.6,
    )
    d1 = eie.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        current_price=99.5,
        market_state=state,
        path_plan=plan,
    )
    assert d1.features.get("exit_pressure_streak", 0) >= 1
