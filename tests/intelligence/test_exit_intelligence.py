"""Unit tests for Exit Intelligence Engine (Phase D)."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.intelligence.exit_intelligence import (
    ExitAction,
    ExitDecision,
    ExitIntelligenceEngine,
)
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
from aitos.intelligence.path_planner.models import PathDestination, PathPlan


def _state(
    *,
    structure: StructureBias = StructureBias.BULLISH,
    of: OrderFlowBias = OrderFlowBias.BUYER_DOMINANT,
    momentum: MomentumState = MomentumState.STRONG,
    reversal_risk: float = 0.15,
) -> MarketState:
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        mid_price=79400.0,
        regime=Regime.TRENDING_UP,
        trend_strength=0.75,
        volatility_regime=VolatilityRegime.NORMAL,
        auction_state=AuctionState.ACCEPTANCE_ABOVE_VALUE,
        order_flow_bias=of,
        liquidity_bias=LiquidityBias.UPSIDE_LIQUIDITY_HIGH,
        momentum=momentum,
        structure=structure,
        reversal_risk=reversal_risk,
    )


def _plan_with_upside(prob: float = 0.70) -> PathPlan:
    return PathPlan(
        symbol="BTCUSDT",
        current_price=79400.0,
        upside=(
            PathDestination(
                price=79800.0,
                probability=prob,
                distance=400.0,
                market_structure_type="prior_high",
                liquidity_type="none",
                expected_horizon="intraday",
                confidence=0.7,
            ),
        ),
        downside=(
            PathDestination(
                price=79000.0,
                probability=0.25,
                distance=400.0,
                market_structure_type="swing",
                liquidity_type="none",
                expected_horizon="scalp",
                confidence=0.6,
            ),
        ),
        as_of=datetime.now(timezone.utc),
    )


def test_hold_when_thesis_intact():
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        current_price=79400.0,
        market_state=_state(),
        path_plan=_plan_with_upside(0.75),
    )
    assert isinstance(decision, ExitDecision)
    assert decision.action == ExitAction.HOLD
    assert decision.exit_score < 0.65


def test_exit_when_structure_and_of_against():
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        current_price=79200.0,
        market_state=_state(
            structure=StructureBias.BROKEN,
            of=OrderFlowBias.SELLER_DOMINANT,
            momentum=MomentumState.EXHAUSTED,
            reversal_risk=0.75,
        ),
        path_plan=_plan_with_upside(0.15),
    )
    assert decision.action == ExitAction.EXIT
    assert decision.exit_score >= 0.65
    codes = {r.code for r in decision.reasons}
    assert "structure_broken" in codes or "of_reversal" in codes


def test_momentum_alone_does_not_force_exit():
    """Architectural rule: momentum slowdown alone is never enough for EXIT."""
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        current_price=79400.0,
        market_state=_state(
            structure=StructureBias.BULLISH,
            of=OrderFlowBias.BUYER_DOMINANT,
            momentum=MomentumState.EXHAUSTED,  # only this is weak
            reversal_risk=0.20,
        ),
        path_plan=_plan_with_upside(0.70),
    )
    assert decision.action != ExitAction.EXIT
    assert decision.action in (ExitAction.HOLD, ExitAction.MANAGE)


def test_manage_on_moderate_warning():
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        current_price=79300.0,
        market_state=_state(
            structure=StructureBias.BULLISH,
            of=OrderFlowBias.NEUTRAL,
            momentum=MomentumState.WEAK,
            reversal_risk=0.45,
        ),
        path_plan=_plan_with_upside(0.40),
    )
    # Should be MANAGE or HOLD, not forced EXIT
    assert decision.action in (ExitAction.HOLD, ExitAction.MANAGE)
    if decision.action == ExitAction.MANAGE:
        assert 0.0 < decision.suggested_reduce_fraction <= 1.0


def test_exit_confirmation_history_is_isolated_per_trade():
    eng = ExitIntelligenceEngine()
    state = _state(
        structure=StructureBias.BROKEN,
        of=OrderFlowBias.SELLER_DOMINANT,
        momentum=MomentumState.EXHAUSTED,
        reversal_risk=0.75,
    )
    for trade_id in ("trade-a", "trade-b"):
        eng.evaluate(
            symbol="BTCUSDT",
            side="LONG",
            trade_id=trade_id,
            entry_price=79000.0,
            current_price=79200.0,
            market_state=state,
            path_plan=_plan_with_upside(0.15),
        )
    assert eng._exit_pressure_streak["trade-a"] == 1
    assert eng._exit_pressure_streak["trade-b"] == 1
    eng.reset_trade("trade-a")
    assert "trade-a" not in eng._exit_pressure_streak
    assert eng._exit_pressure_streak["trade-b"] == 1


def test_manage_requires_independent_structure_and_orderflow_confirmation():
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        trade_id="trade-confirmed",
        entry_price=79000.0,
        current_price=79200.0,
        market_state=_state(
            structure=StructureBias.BEARISH,
            of=OrderFlowBias.SELLER_DOMINANT,
            momentum=MomentumState.WEAK,
            reversal_risk=0.65,
        ),
        path_plan=_plan_with_upside(0.15),
    )
    assert decision.action == ExitAction.MANAGE
    assert decision.features["manage_confirmation_groups"] >= 2


def test_momentum_only_does_not_authorize_partial_reduction():
    eng = ExitIntelligenceEngine()
    decision = eng.evaluate(
        symbol="BTCUSDT",
        side="LONG",
        trade_id="trade-momentum-only",
        entry_price=79000.0,
        current_price=79100.0,
        market_state=_state(
            structure=StructureBias.BULLISH,
            of=OrderFlowBias.BUYER_DOMINANT,
            momentum=MomentumState.EXHAUSTED,
            reversal_risk=0.20,
        ),
        path_plan=_plan_with_upside(0.70),
    )
    assert decision.action == ExitAction.HOLD
    assert decision.suggested_reduce_fraction == 0.0
