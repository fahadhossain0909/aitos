from aitos.intelligence.footprint_signals import FootprintSignals
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.orderflow_liquidity_interaction import \
    FlowLiquidityInteractionEngine


def fp(delta=8.0, imbalance=8.0, absorption=0.0, bias="bullish"):
    return FootprintSignals(delta, imbalance, absorption, 0.0, bias)


def test_buy_absorption_proxy():
    result = FlowLiquidityInteractionEngine().evaluate(
        fp(absorption=7.0),
        [LiquidityEvent("stacking", "ask", 8.0, 100.0, "replenish")],
    )
    assert result.kind == "buy_absorption_proxy"
    assert result.direction == "bearish"


def test_sell_sweep_requires_bearish_flow():
    result = FlowLiquidityInteractionEngine().evaluate(
        fp(delta=2.0, imbalance=2.0, bias="bearish"),
        [LiquidityEvent("sweep", "bid", 8.0, 99.0, "removed")],
    )
    assert result.kind == "sell_side_sweep"
    assert result.direction == "bearish"


def test_liquidity_pull_without_confirmation_is_not_signal():
    result = FlowLiquidityInteractionEngine().evaluate(
        fp(),
        [LiquidityEvent("pulling", "ask", 8.0, 100.0, "removed")],
    )
    assert result.kind == "none"
    assert result.direction == "neutral"
