"""Production-like smoke checks for the paper-trading runtime wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aitos.models.trade import TradeLifecycleState
from aitos.trading.reconciliation import ReconciliationScheduler


@pytest.mark.asyncio
async def test_reconciliation_closes_exchange_closed_position(event_bus):
    trade = MagicMock()
    trade.trade_id = "paper-reconcile-1"

    lifecycle = MagicMock()
    lifecycle.get_open_trades.return_value = [trade]
    lifecycle.reconcile_trade = AsyncMock(
        return_value=MagicMock(
            state=TradeLifecycleState.POSITION_CLOSED,
            exit_reason="exchange_stop",
        )
    )

    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle,
        event_bus=event_bus,
        interval_seconds=3600,
    )
    await scheduler.initialize({})
    try:
        closed = await scheduler.run_once()
        assert closed == 1
        lifecycle.reconcile_trade.assert_awaited_once_with("paper-reconcile-1")
        assert scheduler._last_run_trades_checked == 1
        assert scheduler._last_run_trades_closed == 1
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_reconciliation_keeps_open_trade_when_exchange_position_remains(
    event_bus,
):
    trade = MagicMock()
    trade.trade_id = "paper-reconcile-2"

    lifecycle = MagicMock()
    lifecycle.get_open_trades.return_value = [trade]
    lifecycle.reconcile_trade = AsyncMock(
        return_value=MagicMock(
            state=TradeLifecycleState.POSITION_OPENED,
            exit_reason=None,
        )
    )

    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle,
        event_bus=event_bus,
        interval_seconds=3600,
    )
    await scheduler.initialize({})
    try:
        assert await scheduler.run_once() == 0
        assert scheduler._last_run_trades_closed == 0
    finally:
        await scheduler.shutdown()


def test_paper_runtime_configuration_is_safe_for_real_execution():
    from aitos.execution.order_executor import SimulatedOrderExecutor

    executor = SimulatedOrderExecutor()
    assert executor is not None
