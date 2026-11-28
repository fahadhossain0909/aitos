import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


class _DurableLiveExecutor:
    requires_exchange_execution = True
    execution_intent_store = object()


class _SchedulerLifecycle:
    def __init__(
        self,
        executor,
        trade_ids=("trade-1", "trade-2"),
        algo_trade_ids=(),
    ):
        self._order_executor = executor
        self._account_reconciliation_error = False
        self._trades = [
            SimpleNamespace(
                trade_id=trade_id,
                sl_order_id=(
                    f"algo:{trade_id}" if trade_id in algo_trade_ids else None
                ),
                tp_order_ids=[],
            )
            for trade_id in trade_ids
        ]
        self.reconcile_calls = []

    def get_open_trades(self):
        return list(self._trades)

    async def reconcile_trade(self, trade_id):
        self.reconcile_calls.append(trade_id)
        return SimpleNamespace(
            state=TradeLifecycleState.POSITION_OPENED,
            exit_reason=None,
        )


@pytest.mark.asyncio
async def test_background_child_partial_fill_skips_duplicate_trade_exit(monkeypatch):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(_DurableLiveExecutor(), algo_trade_ids=("trade-1",))
    bus = SimpleNamespace(publish=AsyncMock())
    recover = AsyncMock(
        return_value={
            "applied": [
                {
                    "trade_id": "trade-1",
                    "terminal": False,
                    "delta_quantity": "0.25",
                }
            ],
            "blocked": [],
            "safe_to_resume": False,
        }
    )
    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        recover,
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        assert await scheduler.run_once() == 0
        recover.assert_awaited_once_with(
            lifecycle._order_executor,
            lifecycle._order_executor.execution_intent_store,
            lifecycle,
        )
        assert lifecycle.reconcile_calls == ["trade-2"]
        assert lifecycle._account_reconciliation_error is True
        payload = bus.publish.await_args.args[0].payload
        assert payload["protective_child_fills_applied"] == 1
        assert payload["protective_child_fills_blocked"] == 1
        assert payload["handled_trade_ids"] == ["trade-1"]
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_background_ambiguous_child_quarantines_only_its_trade(monkeypatch):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(_DurableLiveExecutor(), algo_trade_ids=("trade-1",))
    bus = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        AsyncMock(
            return_value={
                "applied": [],
                "blocked": [
                    {
                        "trade_id": "trade-1",
                        "order_id": "algo:123",
                        "reason": "child identity mismatch",
                    }
                ],
                "safe_to_resume": False,
            }
        ),
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        await scheduler.run_once()
        assert lifecycle.reconcile_calls == ["trade-2"]
        assert lifecycle._account_reconciliation_error is True
        assert scheduler._last_child_fills_blocked == 1
        health = await scheduler.health_check()
        assert health.status.value == "degraded"
        assert health.details["account_reconciliation_error"] is True
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_background_recovery_exception_skips_all_live_trade_exits(monkeypatch):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(
        _DurableLiveExecutor(),
        algo_trade_ids=("trade-1", "trade-2"),
    )
    bus = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        AsyncMock(side_effect=OSError("venue inventory unavailable")),
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        await scheduler.run_once()
        assert lifecycle.reconcile_calls == []
        assert lifecycle._account_reconciliation_error is True
        assert scheduler._last_child_fills_blocked == 2
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_paper_background_reconciliation_skips_exchange_child_recovery(
    monkeypatch,
):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(FakeExchangeCapableExecutor())
    bus = SimpleNamespace(publish=AsyncMock())
    recover = AsyncMock()
    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        recover,
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        await scheduler.run_once()
        recover.assert_not_awaited()
        assert lifecycle.reconcile_calls == ["trade-1", "trade-2"]
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_overlapping_background_and_manual_recovery_passes_are_serialized(
    monkeypatch,
):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(_DurableLiveExecutor(), trade_ids=())
    bus = SimpleNamespace(publish=AsyncMock())
    active = 0
    peak_active = 0

    async def delayed_recovery(*_args):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"applied": [], "blocked": [], "safe_to_resume": False}

    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        delayed_recovery,
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        await asyncio.gather(scheduler.run_once(), scheduler.run_once())
        assert peak_active == 1
        assert scheduler._total_runs == 2
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_live_algo_trade_never_falls_through_to_generic_reconcile_between_checks(
    monkeypatch,
):
    import aitos.trading.reconciliation as reconciliation_module

    lifecycle = _SchedulerLifecycle(_DurableLiveExecutor(), algo_trade_ids=("trade-1",))
    bus = SimpleNamespace(publish=AsyncMock())
    recover = AsyncMock(
        return_value={"applied": [], "blocked": [], "safe_to_resume": False}
    )
    monkeypatch.setattr(
        reconciliation_module,
        "recover_triggered_protection_child_fills",
        recover,
    )
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=3600)
    await scheduler.initialize({})
    try:
        await scheduler.run_once()
        assert lifecycle.reconcile_calls == ["trade-2"]
        await scheduler.run_once()
        assert lifecycle.reconcile_calls == ["trade-2", "trade-2"]
        recover.assert_awaited_once()
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_reconciliation_loop_backoff_and_fail_closed_gate(monkeypatch):
    lifecycle = _SchedulerLifecycle(FakeExchangeCapableExecutor(), trade_ids=())
    bus = SimpleNamespace(publish=AsyncMock())
    scheduler = ReconciliationScheduler(lifecycle, bus, interval_seconds=0.25)
    scheduler._initialized = True
    delays = []

    async def controlled_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    async def fail_once():
        raise OSError("reconciliation store unavailable")

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(scheduler, "run_once", fail_once)

    await scheduler._run_loop()

    assert delays == [0.25, 0.5, 1.0, 2.0]
    assert scheduler._consecutive_failures == 3
    assert lifecycle._account_reconciliation_error is True
    assert scheduler._last_failure_at is not None
    health = await scheduler.health_check()
    assert health.details["consecutive_failures"] == 3
    assert health.status.value == "unhealthy"
