"""Composed lifecycle/capital/SQLite contracts with an OFFLINE venue responder.

This is not an exchange or ClickHouse integration certification.
"""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from aitos.core.contracts import ModuleStatus
from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.execution.intent_store import ExecutionIntentStore, ExecutionUncertainError
from aitos.models.trade import Opportunity, TradeSide, TradeLifecycleState
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.persistent_state import IdempotentOrderExecutor


def opportunity():
    return Opportunity(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        stop_loss_price=98,
        take_profit_levels=[106],
        confidence=0.9,
        strategy_id="isolated",
        rationale="composed regression",
    )


async def setup(
    event_bus, risk_engine, tmp_path, *, delayed=False, stop_failure=False, fraction=1
):
    store = ExecutionIntentStore(tmp_path / "orders.sqlite3", scope="isolated-account")
    raw = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=store)
    started, release = asyncio.Event(), asyncio.Event()
    if not delayed:
        release.set()
    calls = []

    async def venue(method, path, params):
        calls.append((method, path, params.copy()))
        if params.get("type") == "MARKET":
            started.set()
            await release.wait()
            return dict(
                orderId=10,
                status="FILLED" if fraction == 1 else "CANCELED",
                executedQty=str(params["quantity"] * fraction),
                avgPrice="100",
            )
        if params.get("type") == "STOP_MARKET":
            return dict(algoId=20, algoStatus="REJECTED" if stop_failure else "NEW")
        if params.get("type") == "TAKE_PROFIT_MARKET":
            return dict(algoId=30, algoStatus="NEW")
        raise AssertionError("unexpected offline venue call")

    raw._signed_request = AsyncMock(side_effect=venue)
    wrapper = IdempotentOrderExecutor(raw)
    value = TradeLifecycle(
        event_bus, risk_engine, wrapper, use_exchange_side_stops=True
    )
    await value.initialize({})
    snapshots = []

    async def persist(trade):
        snapshots.append(copy.deepcopy(trade.to_dict()))

    value._execution_position_verifier = AsyncMock()
    value._execution_state_writer = AsyncMock(side_effect=persist)
    return value, store, calls, snapshots, started, release


def portfolio():
    return PortfolioState(
        equity_usd=10000, peak_equity_usd=10000, volatility_percentile=30
    )


async def test_cancelled_caller_does_not_abandon_capital_protection_or_application(
    event_bus, risk_engine, tmp_path
):
    value, store, calls, snapshots, started, release = await setup(
        event_bus, risk_engine, tmp_path, delayed=True
    )
    caller = asyncio.create_task(value.submit_opportunity(opportunity(), portfolio()))
    await asyncio.wait_for(started.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert store.unresolved()[0]["state"] == "SUBMITTING"
    release.set()
    await value.drain_live_submissions(2)
    assert len(value.get_open_trades()) == 1
    trade = value.get_open_trades()[0]
    assert trade.sl_order_id == "algo:20"
    assert trade.tp_order_ids == ["algo:30"]
    assert snapshots[-1]["sl_order_id"] == "algo:20"
    assert store.unresolved() == []
    assert value._aitos_capital_gateway.reservation.get("BTCUSDT") is not None
    assert [call[2]["type"] for call in calls] == [
        "MARKET",
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
    ]
    await value.shutdown()


async def test_protection_failure_keeps_capital_and_durable_fill_unapplied(
    event_bus, risk_engine, tmp_path
):
    value, store, calls, snapshots, _, _ = await setup(
        event_bus, risk_engine, tmp_path, stop_failure=True
    )
    with pytest.raises(ExecutionUncertainError):
        await value.submit_opportunity(opportunity(), portfolio())
    assert value.get_open_trades()[0].state == TradeLifecycleState.POSITION_OPENED
    assert store.unresolved()[0]["state"] == "CONFIRMED"
    assert snapshots  # filled position durably owned before stop-placement attempt
    assert value._aitos_capital_gateway.reservation.get("BTCUSDT") is not None
    assert (await value.health_check()).status == ModuleStatus.UNHEALTHY
    await value.shutdown()


async def test_terminal_partial_entry_sizes_actual_exposure_and_protection(
    event_bus, risk_engine, tmp_path
):
    value, store, calls, _, _, _ = await setup(
        event_bus, risk_engine, tmp_path, fraction=0.5
    )
    trade = await value.submit_opportunity(opportunity(), portfolio())
    requested = calls[0][2]["quantity"]
    assert trade.quantity == requested * 0.5
    assert trade.position_size_usd == trade.quantity * 100
    assert trade.risk_amount_usd == trade.quantity * 2
    assert calls[1][2]["quantity"] == pytest.approx(round(trade.quantity, 3), abs=0.001)
    assert not store.unresolved()
    await value.shutdown()


async def test_missing_persistence_fails_before_any_venue_call(
    event_bus, risk_engine, tmp_path
):
    value, store, calls, _, _, _ = await setup(event_bus, risk_engine, tmp_path)
    del value._execution_state_writer
    with pytest.raises(RuntimeError, match="durable"):
        await value.submit_opportunity(opportunity(), portfolio())
    assert not calls
    assert not store.unresolved()
    assert value._aitos_capital_gateway.reservation.get("BTCUSDT") is None
    await value.shutdown()


async def test_shutdown_drain_timeout_does_not_cancel_owner_task(
    event_bus, risk_engine, tmp_path
):
    value, store, _, _, started, release = await setup(
        event_bus, risk_engine, tmp_path, delayed=True
    )
    caller = asyncio.create_task(value.submit_opportunity(opportunity(), portfolio()))
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(ExecutionUncertainError, match="did not drain"):
        await value.drain_live_submissions(0.01)
    assert not caller.done()
    with pytest.raises(RuntimeError, match="draining"):
        await value.submit_opportunity(opportunity(), portfolio())
    release.set()
    assert (
        await asyncio.wait_for(caller, 2)
    ).state == TradeLifecycleState.POSITION_OPENED
    await value.shutdown()


async def test_account_mismatch_retains_fill_evidence_and_capital(
    event_bus, risk_engine, tmp_path
):
    value, store, _, snapshots, _, _ = await setup(event_bus, risk_engine, tmp_path)
    value._execution_position_verifier.side_effect = ValueError(
        "account exposure mismatch"
    )
    with pytest.raises(ValueError, match="mismatch"):
        await value.submit_opportunity(opportunity(), portfolio())
    assert store.unresolved()[0]["state"] == "CONFIRMED"
    assert snapshots[-1]["sl_order_id"] == "algo:20"
    assert value._aitos_capital_gateway.reservation.get("BTCUSDT") is not None
    assert (await value.health_check()).status == ModuleStatus.UNHEALTHY
    await value.shutdown()
