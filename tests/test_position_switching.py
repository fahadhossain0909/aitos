from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from aitos.execution.order_executor import SimulatedOrderExecutor
from aitos.models.trade import (
    Opportunity,
    Trade,
    TradeLifecycleState,
    TradeSide,
)
from aitos.trading.position_switching import (
    CloseReceipt,
    OrderExecutorCloseAdapter,
    PositionSwitchCoordinator,
    SQLiteSwitchJournal,
)


def _trade() -> Trade:
    return Trade(
        trade_id="trade-old",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=2.0,
        position_size_usd=100.0,
        risk_amount_usd=1.0,
        strategy_id="test",
        agent_consensus={},
        explanation="incumbent",
        sl_price=99.0,
        tp_price=103.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-05T00:00:00+00:00",
    )


def _candidate() -> Opportunity:
    return Opportunity(
        symbol="ETHUSDT",
        side=TradeSide.LONG,
        entry_price=50.0,
        stop_loss_price=49.0,
        take_profit_levels=[53.0],
        confidence=0.95,
        strategy_id="test",
        rationale="replacement",
        opportunity_id="opp-new",
    )


class _Lifecycle:
    def __init__(self):
        self.open_trades = [_trade()]
        self.closed_trades = []
        self.open_calls = 0
        self.close_calls = 0

    def get_open_trades(self):
        return list(self.open_trades)

    def get_closed_trades(self):
        return list(self.closed_trades)

    async def close_trade(self, trade_id, price, reason):
        self.close_calls += 1
        trade = self.open_trades.pop(0)
        trade.state = TradeLifecycleState.POSITION_CLOSED
        self.closed_trades.append(trade)
        return trade

    async def submit_opportunity(self, opportunity, portfolio):
        self.open_calls += 1
        trade = _trade()
        trade.trade_id = "trade-new"
        trade.symbol = opportunity.symbol
        trade.agent_consensus = dict(opportunity.agent_consensus)
        trade.state = TradeLifecycleState.POSITION_OPENED
        self.open_trades.append(trade)
        return trade


class _Gateway:
    def __init__(self):
        self.released = []

    def release(self, symbol):
        self.released.append(symbol)


class _Close:
    def __init__(self):
        self.calls = 0

    async def __call__(self, trade, client_order_id, existing_order_id):
        self.calls += 1
        return CloseReceipt(True, "order-close-1", 101.0, trade.quantity, True)


@dataclass
class _Decision:
    should_switch: bool = True


@pytest.mark.asyncio
async def test_reduce_only_adapter_requires_filled_order_and_flat_position():
    executor = SimulatedOrderExecutor()
    adapter = OrderExecutorCloseAdapter(executor, lambda symbol: True)
    receipt = await adapter(_trade(), "stable-close-id", None)
    assert receipt.confirmed
    assert receipt.position_flat
    assert receipt.filled_quantity == 1.0
    status = await executor.get_order_by_client_id("BTCUSDT", "stable-close-id")
    assert status["reduceOnly"] is True


@pytest.mark.asyncio
async def test_coordinator_orders_close_confirmation_release_then_open(tmp_path: Path):
    lifecycle = _Lifecycle()
    gateway = _Gateway()
    close = _Close()
    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=gateway,
        journal=SQLiteSwitchJournal(tmp_path / "switches.sqlite"),
        close_and_confirm=close,
        portfolio_provider=lambda: object(),
    )
    result = await coordinator.switch(
        incumbent=lifecycle.open_trades[0],
        candidate=_candidate(),
        decision=_Decision(),
    )
    assert result is not None
    assert result.symbol == "ETHUSDT"
    assert lifecycle.close_calls == 1
    assert gateway.released == ["BTCUSDT"]
    assert lifecycle.open_calls == 1
    assert close.calls == 1


@pytest.mark.asyncio
async def test_completed_switch_is_idempotent(tmp_path: Path):
    lifecycle = _Lifecycle()
    gateway = _Gateway()
    close = _Close()
    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=gateway,
        journal=SQLiteSwitchJournal(tmp_path / "switches.sqlite"),
        close_and_confirm=close,
        portfolio_provider=lambda: object(),
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()
    first = await coordinator.switch(
        incumbent=incumbent, candidate=candidate, decision=_Decision()
    )
    second = await coordinator.switch(
        incumbent=incumbent, candidate=candidate, decision=_Decision()
    )
    assert first is second
    assert lifecycle.close_calls == 1
    assert lifecycle.open_calls == 1
    assert close.calls == 1


@pytest.mark.asyncio
async def test_close_not_confirmed_never_releases_or_opens(tmp_path: Path):
    lifecycle = _Lifecycle()
    gateway = _Gateway()

    async def pending_close(trade, client_order_id, existing_order_id):
        return CloseReceipt(False, "pending-order", 0.0, 0.0, False)

    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=gateway,
        journal=SQLiteSwitchJournal(tmp_path / "switches.sqlite"),
        close_and_confirm=pending_close,
        portfolio_provider=lambda: object(),
    )
    result = await coordinator.switch(
        incumbent=lifecycle.open_trades[0],
        candidate=_candidate(),
        decision=_Decision(),
    )
    assert result is None
    assert gateway.released == []
    assert lifecycle.open_calls == 0
    assert lifecycle.close_calls == 0
