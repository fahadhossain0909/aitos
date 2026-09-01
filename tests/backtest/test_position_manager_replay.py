from datetime import datetime, timezone

from aitos.backtest.hedge_metrics import excursions
from aitos.intelligence.exit_intelligence.models import ExitAction
from aitos.intelligence.hedge_intelligence import HedgeAction, HedgeIntelligenceEngine
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
from aitos.intelligence.trade_thesis.models import ThesisEvaluation, ThesisHealth


def _state(side: str, adverse: bool) -> MarketState:
    long_side = side == "LONG"
    return MarketState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        mid_price=100.0,
        regime=Regime.TRANSITION,
        trend_strength=0.2,
        volatility_regime=(
            VolatilityRegime.EXPANDING if adverse else VolatilityRegime.NORMAL
        ),
        auction_state=AuctionState.BALANCED,
        order_flow_bias=(
            (
                OrderFlowBias.SELLER_DOMINANT
                if long_side
                else OrderFlowBias.BUYER_DOMINANT
            )
            if adverse
            else (
                OrderFlowBias.BUYER_DOMINANT
                if long_side
                else OrderFlowBias.SELLER_DOMINANT
            )
        ),
        liquidity_bias=(
            (
                LiquidityBias.DOWNSIDE_LIQUIDITY_HIGH
                if long_side
                else LiquidityBias.UPSIDE_LIQUIDITY_HIGH
            )
            if adverse
            else LiquidityBias.BALANCED
        ),
        momentum=MomentumState.WEAK if adverse else MomentumState.STRONG,
        structure=(
            (StructureBias.BEARISH if long_side else StructureBias.BULLISH)
            if adverse
            else (StructureBias.BULLISH if long_side else StructureBias.BEARISH)
        ),
        reversal_risk=0.8 if adverse else 0.1,
    )


def _thesis() -> ThesisEvaluation:
    return ThesisEvaluation(
        health=ThesisHealth.INTACT,
        consistency_score=0.8,
        breached_invalidations=(),
        lost_confirmations=(),
        active_confirmations=(),
    )


def test_hedge_engine_opens_only_above_threshold() -> None:
    engine = HedgeIntelligenceEngine({"enabled": True})
    decision = engine.evaluate(
        symbol="BTCUSDT",
        primary_side="LONG",
        market_state=_state("LONG", True),
        thesis_eval=_thesis(),
        exit_action=ExitAction.MANAGE,
        current_price=99.0,
        primary_entry_price=100.0,
        primary_r_distance=2.0,
        timestamp=datetime.now(timezone.utc),
    )
    assert decision.action == HedgeAction.OPEN
    assert decision.hedge_side == "SHORT"
    assert 0.25 <= decision.hedge_ratio <= 0.50


def test_excursion_normalizes_signed_mae_mfe() -> None:
    result = excursions(100.0, "LONG", [100.0, 98.0, 103.0])
    assert result.mae == -0.02
    assert result.mfe == 0.03
