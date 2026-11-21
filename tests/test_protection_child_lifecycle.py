import copy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from aitos.execution.intent_store import ExecutionIntentStore, ExecutionRecoveryRequired
from aitos.execution.order_executor import OrderRequest, OrderResult
from aitos.execution.protection_reconciliation import (
    recover_triggered_protection_child_fills,
)
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.lifecycle import TradeLifecycle


class TriggeredChildExecutor:
    requires_exchange_execution = True
    supports_exchange_side_stops = True
    execution_scope = "child-account"
    hedge_mode = False

    def __init__(self, journal):
        self.intent_store = journal
        self.statuses = {"algo:123": "FINISHED"}
        self.cancelled = []

    async def place_stop_loss_order(
        self,
        symbol,
        side,
        quantity,
        stop_price,
        *,
        owner_trade_id=None,
        client_order_id=None,
    ):
        request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=stop_price,
            order_type="STOP_MARKET",
            client_order_id=client_order_id,
            reduce_only=True,
            owner_trade_id=owner_trade_id,
        )
        self.intent_store.begin(request)
        result = OrderResult(
            "algo:999",
            symbol,
            side,
            0.0,
            stop_price,
            client_order_id=client_order_id,
        )
        self.intent_store.record_protection(client_order_id, result)
        self.statuses[result.order_id] = "NEW"
        return result

    async def cancel_resting_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        self.statuses[order_id] = "CANCELED"

    def acknowledge_protection(self, result, trade_id):
        self.intent_store.mark_applied(result.client_order_id, trade_id)


def make_trade():
    return Trade(
        trade_id="trade-child-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=2.0,
        position_size_usd=100.0,
        risk_amount_usd=10.0,
        strategy_id="test",
        agent_consensus={},
        explanation="test protective child fill",
        sl_price=90.0,
        tp_price=110.0,
        sl_order_id="algo:123",
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-20T00:00:00+00:00",
    )


def seed_stop(journal):
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="stop-child-client",
            reduce_only=True,
            owner_trade_id="trade-child-1",
        )
    )
    journal.record_protection(
        "stop-child-client",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="stop-child-client",
        ),
    )
    journal.mark_applied("stop-child-client", "trade-child-1")


def algo_snapshot():
    return {
        "algoId": 123,
        "actualOrderId": 456,
        "clientAlgoId": "stop-child-client",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "algoStatus": "FINISHED",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "closePosition": False,
    }


def child_snapshot(*, status="PARTIALLY_FILLED", quantity="0.25", quote="22.5"):
    return {
        "orderId": 456,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "status": status,
        "origQty": "1.0",
        "executedQty": quantity,
        "cumQuote": quote,
        "avgPrice": "90",
    }


@pytest.mark.asyncio
async def test_child_fill_applies_only_new_cumulative_delta_and_replays_idempotently(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(tmp_path / "child.sqlite", scope="child-account")
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    lifecycle._open_trades[trade.trade_id] = trade
    snapshots = []

    async def persist(value):
        snapshots.append(value.to_dict())

    lifecycle._execution_state_writer = persist
    lifecycle._execution_position_verifier = AsyncMock()

    first = await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id, algo_snapshot(), child_snapshot()
    )
    assert first["delta_quantity"] == "0.25"
    assert trade.quantity == pytest.approx(0.75)
    assert trade.position_size_usd == pytest.approx(75.0)
    assert trade.gross_realized_pnl_usd == pytest.approx(-2.5)
    assert (
        journal.protection_child_watermark("123", "456")["cumulative_quantity"]
        == "0.25"
    )

    second = await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id,
        algo_snapshot(),
        child_snapshot(quantity="0.5", quote="45.0"),
    )
    assert second["delta_quantity"] == "0.25"
    assert trade.quantity == pytest.approx(0.5)
    assert trade.gross_realized_pnl_usd == pytest.approx(-5.0)

    replay = await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id,
        algo_snapshot(),
        child_snapshot(quantity="0.5", quote="45.0"),
    )
    assert Decimal(replay["delta_quantity"]) == Decimal("0")
    assert trade.quantity == pytest.approx(0.5)
    assert len(trade.partial_exits) == 2
    assert (
        snapshots[-1]["agent_consensus"]["_applied_protection_child_fills"]["123:456"][
            "cumulative_quantity"
        ]
        == "0.5"
    )


@pytest.mark.asyncio
async def test_terminal_partial_stop_fill_reprotects_remaining_position(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "child-terminal.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade

    async def persist(_trade):
        return None

    lifecycle._execution_state_writer = persist
    lifecycle._execution_position_verifier = AsyncMock()

    result = await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id,
        algo_snapshot(),
        child_snapshot(status="CANCELED"),
    )

    assert result["terminal"] is True
    assert trade.quantity == pytest.approx(0.75)
    assert trade.sl_order_id == "algo:999"
    assert executor.cancelled == ["algo:123"]
    assert trade.agent_consensus.get("_pending_stop_cancellations", []) == []
    assert journal.protection_handovers()[0]["state"] == "APPLIED"
    assert (
        journal.protection_child_watermark("123", "456")["cumulative_quantity"]
        == "0.25"
    )


@pytest.mark.asyncio
async def test_snapshot_ahead_of_sqlite_watermark_recovers_without_double_reduction(
    event_bus, risk_engine, tmp_path, monkeypatch
):
    journal = ExecutionIntentStore(
        tmp_path / "child-crash.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    lifecycle._open_trades[trade.trade_id] = trade
    persisted = []

    async def persist(value):
        persisted.append(value.to_dict())

    lifecycle._execution_state_writer = persist
    lifecycle._execution_position_verifier = AsyncMock()
    original_mark = journal.mark_protection_child_applied
    monkeypatch.setattr(
        journal,
        "mark_protection_child_applied",
        lambda **kwargs: (_ for _ in ()).throw(OSError("simulated SQLite ACK failure")),
    )
    with pytest.raises(OSError, match="SQLite ACK failure"):
        await lifecycle.apply_triggered_protection_child_fill(
            trade.trade_id, algo_snapshot(), child_snapshot()
        )
    assert trade.quantity == pytest.approx(0.75)
    assert (
        persisted[-1]["agent_consensus"]["_applied_protection_child_fills"]["123:456"][
            "cumulative_quantity"
        ]
        == "0.25"
    )

    # Restore from the durable ClickHouse-like snapshot, with SQLite still behind.
    monkeypatch.setattr(journal, "mark_protection_child_applied", original_mark)
    restored = copy.deepcopy(trade)
    restarted = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    restarted._open_trades[restored.trade_id] = restored
    restarted._execution_state_writer = persist
    restarted._execution_position_verifier = AsyncMock()
    replay = await restarted.apply_triggered_protection_child_fill(
        restored.trade_id, algo_snapshot(), child_snapshot()
    )
    assert Decimal(replay["delta_quantity"]) == Decimal("0")
    assert restored.quantity == pytest.approx(0.75)
    assert (
        journal.protection_child_watermark("123", "456")["cumulative_quantity"]
        == "0.25"
    )


@pytest.mark.asyncio
async def test_sqlite_watermark_ahead_of_trade_snapshot_is_quarantined(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "child-ahead.sqlite", scope="child-account"
    )
    seed_stop(journal)
    journal.mark_protection_child_applied(
        algo_id="123",
        child_order_id="456",
        trade_id="trade-child-1",
        symbol="BTCUSDT",
        cumulative_quantity="0.25",
        cumulative_quote="22.5",
        status="PARTIALLY_FILLED",
    )
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=TriggeredChildExecutor(journal),
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    lifecycle._open_trades[trade.trade_id] = trade
    lifecycle._execution_state_writer = AsyncMock()
    lifecycle._execution_position_verifier = AsyncMock()

    with pytest.raises(ExecutionRecoveryRequired, match="ahead"):
        await lifecycle.apply_triggered_protection_child_fill(
            trade.trade_id, algo_snapshot(), child_snapshot()
        )
    assert trade.quantity == pytest.approx(1.0)
    lifecycle._execution_state_writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_child_recovery_routes_owned_triggered_algo_to_lifecycle(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "child-startup.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    executor.get_algo_order_by_client_id = AsyncMock(return_value=algo_snapshot())
    executor.get_order_status = AsyncMock(return_value=child_snapshot())
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade
    lifecycle.apply_triggered_protection_child_fill = AsyncMock(
        return_value={
            "trade_id": trade.trade_id,
            "algo_order_id": "algo:123",
            "child_order_id": "456",
            "delta_quantity": "0.25",
            "safe_to_resume": False,
        }
    )

    report = await recover_triggered_protection_child_fills(
        executor, journal, lifecycle
    )

    assert report["inspected"] == 1
    assert report["blocked"] == []
    assert report["safe_to_resume"] is False
    assert report["applied"][0]["delta_quantity"] == "0.25"
    lifecycle.apply_triggered_protection_child_fill.assert_awaited_once_with(
        trade.trade_id, algo_snapshot(), child_snapshot()
    )


@pytest.mark.asyncio
async def test_full_terminal_stop_child_fill_closes_without_second_market_order(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "child-full-close.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade
    lifecycle._execution_state_writer = AsyncMock()
    lifecycle._execution_position_verifier = AsyncMock()

    result = await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id,
        algo_snapshot(),
        child_snapshot(status="FILLED", quantity="1.0", quote="90.0"),
    )

    assert result["terminal"] is True
    assert trade.trade_id not in lifecycle._open_trades
    assert (
        lifecycle._closed_trades[-1].exit_reason == "exchange_protective_child_filled"
    )
    assert lifecycle._closed_trades[-1].gross_realized_pnl_usd == pytest.approx(-10.0)
    assert executor.cancelled == ["algo:123"]
    assert (
        journal.protection_child_watermark("123", "456")["cumulative_quantity"] == "1.0"
    )


@pytest.mark.asyncio
async def test_child_fill_finishes_protected_intent_owner_ack_after_trade_snapshot(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "child-protected-intent.sqlite", scope="child-account"
    )
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="stop-child-client",
            reduce_only=True,
            owner_trade_id="trade-child-1",
        )
    )
    journal.record_protection(
        "stop-child-client",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="stop-child-client",
        ),
    )
    executor = TriggeredChildExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade
    lifecycle._execution_state_writer = AsyncMock()
    lifecycle._execution_position_verifier = AsyncMock()

    await lifecycle.apply_triggered_protection_child_fill(
        trade.trade_id, algo_snapshot(), child_snapshot()
    )

    row = journal.protection_intents()[0]
    assert row["state"] == "APPLIED"
    assert row["trade_id"] == trade.trade_id
    assert trade.quantity == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_triggered_child_recovery_is_idempotent_across_repeated_restarts(
    event_bus, risk_engine, tmp_path
):
    from aitos.trading.persistent_state import _trade_from_dict

    journal = ExecutionIntentStore(
        tmp_path / "repeated-restart.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    algo = {**algo_snapshot(), "algoStatus": "TRIGGERED"}
    child = child_snapshot(status="PARTIALLY_FILLED")
    executor.get_algo_order_by_client_id = AsyncMock(return_value=algo)
    executor.get_order_status = AsyncMock(return_value=child)

    durable_snapshot = make_trade().to_dict()
    for restart_number in range(4):
        restored = _trade_from_dict(copy.deepcopy(durable_snapshot))
        lifecycle = TradeLifecycle(
            event_bus=event_bus,
            risk_engine=risk_engine,
            order_executor=executor,
            use_exchange_side_stops=True,
        )
        lifecycle._open_trades[restored.trade_id] = restored
        lifecycle._execution_position_verifier = AsyncMock()

        async def persist(value):
            nonlocal durable_snapshot
            durable_snapshot = value.to_dict()

        lifecycle._execution_state_writer = persist
        report = await recover_triggered_protection_child_fills(
            executor, journal, lifecycle
        )
        assert report["blocked"] == []
        assert len(report["applied"]) == 1
        assert restored.quantity == pytest.approx(0.75)
        assert restored.gross_realized_pnl_usd == pytest.approx(-2.5)
        assert (
            journal.protection_child_watermark("123", "456")["cumulative_quantity"]
            == "0.25"
        )
        assert (
            durable_snapshot["agent_consensus"]["_applied_protection_child_fills"][
                "123:456"
            ]["cumulative_quantity"]
            == "0.25"
        )
        if restart_number:
            assert restored.quantity == pytest.approx(0.75)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate_algo,mutate_child",
    [
        (lambda algo: algo.update(clientAlgoId="foreign-client"), lambda child: None),
        (lambda algo: algo.update(actualOrderId=999), lambda child: None),
        (lambda algo: None, lambda child: child.update(orderId=999)),
        (lambda algo: None, lambda child: child.update(symbol="ETHUSDT")),
        (lambda algo: None, lambda child: child.update(side="BUY")),
        (lambda algo: None, lambda child: child.update(origQty="2.0")),
        (lambda algo: None, lambda child: child.update(positionSide="LONG")),
    ],
)
async def test_ambiguous_algo_or_child_identity_is_quarantined(
    event_bus, risk_engine, tmp_path, mutate_algo, mutate_child
):
    journal = ExecutionIntentStore(
        tmp_path / "ambiguous-child.sqlite", scope="child-account"
    )
    seed_stop(journal)
    executor = TriggeredChildExecutor(journal)
    algo = algo_snapshot()
    child = child_snapshot()
    mutate_algo(algo)
    mutate_child(child)
    executor.get_algo_order_by_client_id = AsyncMock(return_value=algo)
    executor.get_order_status = AsyncMock(return_value=child)

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade
    lifecycle._execution_state_writer = AsyncMock()
    lifecycle._execution_position_verifier = AsyncMock()

    report = await recover_triggered_protection_child_fills(
        executor, journal, lifecycle
    )

    assert report["applied"] == []
    assert len(report["blocked"]) == 1
    assert trade.quantity == pytest.approx(1.0)
    lifecycle._execution_state_writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_durable_algo_identity_blocks_all_child_recovery(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(
        tmp_path / "duplicate-child.sqlite", scope="child-account"
    )
    seed_stop(journal)
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=89.0,
            order_type="STOP_MARKET",
            client_order_id="second-stop-client",
            reduce_only=True,
            owner_trade_id="trade-child-2",
        )
    )
    journal.record_protection(
        "second-stop-client",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            89.0,
            client_order_id="second-stop-client",
        ),
    )
    executor = TriggeredChildExecutor(journal)
    executor.get_algo_order_by_client_id = AsyncMock(return_value=algo_snapshot())
    executor.get_order_status = AsyncMock(return_value=child_snapshot())
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = make_trade()
    trade.sl_order_id = "algo:123"
    lifecycle._open_trades[trade.trade_id] = trade

    report = await recover_triggered_protection_child_fills(
        executor, journal, lifecycle
    )
    assert report["applied"] == []
    assert report["safe_to_resume"] is False
    assert any(
        "duplicate durable protective order identity" in item["reason"]
        for item in report["blocked"]
    )
    executor.get_algo_order_by_client_id.assert_not_awaited()
    assert trade.quantity == pytest.approx(1.0)
