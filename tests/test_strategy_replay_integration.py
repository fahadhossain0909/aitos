from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aitos.backtest.engine import BacktestEngine
from aitos.backtest.replay import ReplayEvent
from aitos.models.trade import TradeSide
from aitos.strategy import (
    CapitalAllocator,
    CapitalRequest,
    Strategy,
    StrategyContext,
    StrategyEngine,
    StrategyFamily,
    StrategyMode,
    StrategyRegistry,
    StrategyResult,
)
from aitos.trading.tp_contract import TakeProfitExecutionContract


@dataclass(frozen=True)
class PriceEvent:
    timestamp: datetime
    price: float
    fields: dict


class ReplayStrategy(Strategy):
    strategy_id = "replay-directional"
    family = StrategyFamily.DIRECTIONAL

    def evaluate(self, context):
        return StrategyResult(
            strategy_id=self.strategy_id,
            family=self.family,
            capital_request=CapitalRequest(
                strategy_id=self.strategy_id,
                requested_notional=2_000.0,
                max_loss=40.0,
                expected_edge=7.0,
                confidence=0.9,
                priority=2.0,
            ),
        )


def test_batch_allocation_and_dynamic_tp_contract_survive_historical_replay():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        PriceEvent(start + timedelta(minutes=i), price, fields)
        for i, (price, fields) in enumerate(
            [
                (100.0, {"phase": "entry"}),
                (110.0, {"phase": "tp_checkpoint"}),
                (108.0, {"phase": "hold"}),
                (107.0, {"phase": "thesis_invalidated"}),
            ]
        )
    ]

    registry = StrategyRegistry()
    registry.register(ReplayStrategy())
    strategy_engine = StrategyEngine(registry, CapitalAllocator(5_000))
    cycle = strategy_engine.run_cycle(
        StrategyContext(
            now_ns=1,
            mode=StrategyMode.PAPER,
            available_capital=3_000,
            risk_budget=100,
            global_regime="trending",
        )
    )
    assert len(cycle.allocations) == 1
    allocated_notional = cycle.allocations[0].notional

    tp_contract = TakeProfitExecutionContract(intelligent=True)
    position = {"quantity": 0.0, "entry": 0.0, "closed": False}

    def strategy(event: ReplayEvent, execution):
        if event.fields["phase"] == "entry" and position["quantity"] == 0:
            position["quantity"] = allocated_notional / event.price
            position["entry"] = event.price
            execution.execute("buy", position["quantity"], event.price)
        elif position["quantity"] and not position["closed"]:
            if tp_contract.reached(TradeSide.LONG, event.price, 110.0):
                # A reached static target is only a checkpoint under EIE.
                assert tp_contract.static_tp_may_close is False
            if event.fields.get("thesis_invalidated"):
                execution.execute("sell", position["quantity"], event.price)
                position["closed"] = True

    result = BacktestEngine(
        initial_cash=5_000, fee_rate=0.0004, slippage_bps=0.0
    ).run(events, strategy, mark_price=lambda event: event.price)

    assert position["closed"] is True
    assert result.metrics.trades == 1
    assert result.metrics.total_fees > 0
    assert result.metrics.final_equity < 5_000
    assert len(result.equity_curve) == len(events)
