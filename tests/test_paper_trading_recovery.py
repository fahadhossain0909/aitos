"""Production-like paper-trading recovery and reconciliation tests."""

from __future__ import annotations

import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.persistent_state import TradeStatePersistence


class FakeStateStore:
    def __init__(self, trades=None):
        self.trades = list(trades or [])

    async def load_open_trades(self):
        return list(self.trades)


class FakeLifecycle:
    def __init__(self):
        self._open_trades = {}


@pytest.mark.asyncio
async def test_paper_trade_survives_process_restart(event_bus):
    trade = Trade(
        trade_id="paper-recovery-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=0.01,
        leverage=1.0,
        position_size_usd=1.0,
        risk_amount_usd=0.05,
        strategy_id="paper",
        agent_consensus={},
        explanation="recovery test",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T00:00:00+00:00",
    )
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([trade])
    )

    assert await persistence.restore() == 1
    restored = lifecycle._open_trades[trade.trade_id]
    assert restored.symbol == trade.symbol
    assert restored.entry_price == trade.entry_price
    assert restored.sl_price == trade.sl_price
    assert restored.state == TradeLifecycleState.POSITION_OPENED


@pytest.mark.asyncio
async def test_restart_does_not_create_duplicate_open_trade(event_bus):
    trade = Trade(
        trade_id="paper-idempotent-1",
        symbol="ETHUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=0.01,
        leverage=1.0,
        position_size_usd=1.0,
        risk_amount_usd=0.05,
        strategy_id="paper",
        agent_consensus={},
        explanation="idempotency test",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T00:00:00+00:00",
    )
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([trade])
    )

    assert await persistence.restore() == 1
    assert await persistence.restore() == 1
    assert len(lifecycle._open_trades) == 1


class FakeExchange:
    def __init__(self, position_qty=0.01):
        self.position_qty = position_qty

    async def get_position(self, symbol):
        return {"symbol": symbol, "quantity": self.position_qty}


@pytest.mark.asyncio
async def test_reconciliation_detects_missing_internal_position():
    exchange = FakeExchange(position_qty=0.01)
    internal_positions = {}
    remote = await exchange.get_position("BTCUSDT")

    assert remote["quantity"] > 0
    assert "BTCUSDT" not in internal_positions
    mismatch = remote["quantity"] > 0 and "BTCUSDT" not in internal_positions
    assert mismatch is True
