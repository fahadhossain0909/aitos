import asyncio

import pytest

from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.reconciliation import ReconciliationScheduler
from tests.test_exchange_side_stops import FakeExchangeCapableExecutor


def make_portfolio(**overrides):
    defaults = dict(
        equity_usd=10_000.0, peak_equity_usd=10_000.0, volatility_percentile=30.0
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def make_opportunity(**overrides):
    defaults = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="test-strategy",
        rationale="test",
        breakeven_at_r_multiple=None,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


async def _wait_for(predicate, timeout=3.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_run_once_with_no_open_trades(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=1000
    )
    await scheduler.initialize({})

    closed = await scheduler.run_once()

    assert closed == 0
    health = await scheduler.health_check()
    assert health.details["last_run_trades_checked"] == 0
    assert health.details["total_runs"] == 1

    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_run_once_closes_trade_with_filled_exchange_stop(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=1000
    )
    await scheduler.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    executor.mark_filled(trade.sl_order_id)  # simulate a fill our live loop never saw

    closed = await scheduler.run_once()

    assert closed == 1
    assert trade.state == TradeLifecycleState.POSITION_CLOSED
    assert trade.exit_reason == "sl_triggered_exchange"
    assert lifecycle.get_open_trades() == []

    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_run_once_leaves_healthy_trades_open(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=1000
    )
    await scheduler.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    closed = await scheduler.run_once()

    assert closed == 0
    assert trade.trade_id in [t.trade_id for t in lifecycle.get_open_trades()]

    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_run_once_publishes_summary_event(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=1000
    )
    await scheduler.initialize({})

    received = []

    async def handler(event):
        received.append(event)

    await event_bus.subscribe("trade.reconciliation_run", handler, group="test")

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    executor.mark_filled(trade.tp_order_ids[0])
    await scheduler.run_once()

    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["trades_checked"] == 1
    assert received[0].payload["trades_closed"] == 1

    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_background_loop_automatically_reconciles(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=0.05
    )
    await scheduler.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    executor.mark_filled(trade.sl_order_id)

    closed = await _wait_for(
        lambda: trade.state == TradeLifecycleState.POSITION_CLOSED, timeout=3.0
    )

    assert closed is True
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_health_check_reports_unhealthy_after_shutdown(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(
        trade_lifecycle=lifecycle, event_bus=event_bus, interval_seconds=1000
    )
    await scheduler.initialize({})

    healthy = await scheduler.health_check()
    assert healthy.status.value == "healthy"

    await scheduler.shutdown()
    unhealthy = await scheduler.health_check()
    assert unhealthy.status.value == "unhealthy"


@pytest.mark.asyncio
async def test_run_once_before_initialize_raises(event_bus, risk_engine):
    from aitos.core.exceptions import ModuleNotInitializedError

    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    scheduler = ReconciliationScheduler(trade_lifecycle=lifecycle, event_bus=event_bus)

    with pytest.raises(ModuleNotInitializedError):
        await scheduler.run_once()
