from aitos.strategy.allocator import Allocation
from aitos.strategy.contracts import (
    ExecutionIntent,
    MarketSnapshot,
    StrategyContext,
    StrategyFamily,
    StrategyMode,
    StrategyResult,
)
from aitos.strategy.engine import StrategyCycle
from aitos.strategy.shadow_bridge import build_strategy_shadow_report


def test_strategy_cycle_maps_to_non_executable_shadow_proposal():
    context = StrategyContext(
        now_ns=123,
        mode=StrategyMode.SHADOW,
        snapshots={
            "binance:spot:BTCUSDT": MarketSnapshot(
                instrument_id="binance:spot:BTCUSDT", mid=100.0
            )
        },
    )
    result = StrategyResult(
        strategy_id="mean-reversion",
        family=StrategyFamily.DIRECTIONAL,
        intents=(
            ExecutionIntent(
                instrument_id="binance:spot:BTCUSDT",
                side="buy",
                quantity=0.5,
                rationale="shadow candidate",
            ),
        ),
    )
    cycle = StrategyCycle(
        regime="range",
        results=(result,),
        allocations=(Allocation("mean-reversion", 80.0, 4.0, 1.0),),
    )
    report = build_strategy_shadow_report(cycle, context)
    assert len(report.proposals) == 1
    proposal = report.proposals[0]
    assert proposal.side == "LONG"
    assert proposal.requested_notional == 50.0
    assert proposal.allocated_notional == 50.0
    assert proposal.execution_eligible is False
    assert report.execution_eligible is False


def test_strategy_shadow_bridge_rejects_unpriced_or_unallocated_intents():
    context = StrategyContext(now_ns=1, mode=StrategyMode.SHADOW)
    result = StrategyResult(
        strategy_id="s",
        family=StrategyFamily.DIRECTIONAL,
        intents=(
            ExecutionIntent(
                instrument_id="missing", side="sell", quantity=1
            ),
        ),
    )
    report = build_strategy_shadow_report(
        StrategyCycle("unknown", (result,), ()), context
    )
    assert report.proposals == ()
    assert report.rejected == (
        ("s:0", "missing point-in-time instrument snapshot"),
    )
