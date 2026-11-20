from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aitos.execution.intent_store import ExecutionIntentStore
from aitos.execution.order_executor import OrderRequest, OrderResult
from aitos.execution.protection_reconciliation import (
    audit_exchange_protection_inventory,
    recover_accepted_initial_stop_submissions,
    recover_protection_handovers,
    require_live_account_exposure_reconciled,
)
from aitos.models.trade import TradeSide

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class ReadOnlyExecutor:
    def __init__(self, scope, orders):
        self.execution_scope = scope
        self.get_open_algo_orders = AsyncMock(return_value=orders)
        self.get_algo_order_by_client_id = AsyncMock(
            side_effect=lambda symbol, client_id: next(
                (dict(row) for row in orders if row.get("clientAlgoId") == client_id),
                None,
            )
        )
        self.get_order_status = AsyncMock(
            return_value={"status": "FILLED", "executedQty": "1.0"}
        )
        self.hedge_mode = False
        self.get_position_risk = AsyncMock(return_value=[])
        self.get_open_orders = AsyncMock(return_value=[])


@pytest.fixture
def protected_intent(tmp_path):
    store = ExecutionIntentStore(tmp_path / "intents.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            "BTCUSDT",
            TradeSide.LONG,
            1.0,
            90.0,
            "STOP_MARKET",
            "aitos-protection-1",
            True,
        )
    )
    return store


def inventory_row():
    return {
        "algoId": 123,
        "clientAlgoId": "aitos-protection-1",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "quantity": "1.0",
        "triggerPrice": "90.0",
        "algoStatus": "NEW",
        "reduceOnly": True,
        "closePosition": False,
    }


@pytest.mark.asyncio
async def test_authenticated_inventory_match_is_observational_only(protected_intent):
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW
    )

    assert report["evidence"] == "AUTHENTICATED_READ_ONLY_EXCHANGE_API"
    assert report["findings"][0]["status"] == "unapplied_match"
    assert report["account_exposure_verified"] is False
    assert report["safe_to_resume"] is False
    assert report["automatic_actions"] == []
    assert protected_intent.unresolved()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_missing_algo_order_does_not_clear_intent_or_claim_no_exposure(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW
    )

    assert report["findings"][0]["status"] == "not_in_open_inventory"
    assert report["safe_to_resume"] is False
    assert protected_intent.unresolved()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_scope_mismatch_fails_before_exchange_inventory_read(protected_intent):
    executor = ReadOnlyExecutor("another-account", [inventory_row()])

    with pytest.raises(ValueError, match="scope mismatch"):
        await audit_exchange_protection_inventory(executor, protected_intent, now=NOW)

    executor.get_open_algo_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_journal_and_empty_inventory_return_non_authorizing_report(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "empty.sqlite", scope="paper-account")
    executor = ReadOnlyExecutor("paper-account", [])

    report = await audit_exchange_protection_inventory(executor, store, now=NOW)

    assert report["findings"] == []
    assert report["safe_to_resume"] is False
    assert report["account_exposure_verified"] is False


@pytest.mark.asyncio
async def test_applied_protection_is_matched_to_its_durable_history(protected_intent):
    protected_intent.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    protected_intent.mark_applied("aitos-protection-1", "durable-trade-1")
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW
    )

    assert report["findings"][0]["status"] == "applied_order_present"
    assert report["safe_to_resume"] is False


@pytest.mark.asyncio
async def test_ambiguous_post_ack_is_queried_by_client_id_without_journal_write(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [])
    executor.get_algo_order_by_client_id = AsyncMock(return_value=inventory_row())

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW
    )

    finding = report["ambiguous_submissions"][0]
    assert finding["status"] == "accepted_resting_order_found"
    assert finding["algo_id"] == "123"
    assert report["safe_to_resume"] is False
    assert protected_intent.protection_intents()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_ambiguous_triggered_algo_queries_child_fill_read_only(protected_intent):
    executor = ReadOnlyExecutor("paper-account", [])
    triggered = inventory_row()
    triggered.update(algoStatus="FINISHED", actualOrderId="456")
    executor.get_algo_order_by_client_id = AsyncMock(return_value=triggered)

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW
    )

    finding = report["ambiguous_submissions"][0]
    assert finding["status"] == "child_order_observed"
    assert finding["child_status"] == "FILLED"
    assert finding["executed_quantity"] == "1.0"
    executor.get_order_status.assert_awaited_once_with("BTCUSDT", "algo:123")
    assert protected_intent.protection_intents()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_account_exposure_audit_verifies_full_position_snapshot(protected_intent):
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    executor.get_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.0",
            }
        ]
    )
    trade = SimpleNamespace(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[trade]
    )

    assert report["position_snapshot_verified"] is True
    assert report["account_exposure_verified"] is True
    assert report["pending_entry_orders"] == []
    executor.get_position_risk.assert_awaited_once()
    executor.get_open_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_exposure_mismatch_is_reported_not_authorized(protected_intent):
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    executor.get_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "ETHUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.0",
            }
        ]
    )
    trade = SimpleNamespace(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[trade]
    )

    assert report["position_snapshot_verified"] is False
    assert "mismatch" in report["position_snapshot_error"]
    assert report["account_exposure_verified"] is False
    assert report["safe_to_resume"] is False


@pytest.mark.asyncio
async def test_unpriced_open_entry_order_prevents_account_exposure_verification(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    executor.get_open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 987,
                "symbol": "ETHUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "status": "NEW",
                "reduceOnly": False,
                "closePosition": False,
            }
        ]
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[]
    )

    assert report["position_snapshot_verified"] is True
    assert report["account_exposure_verified"] is False
    assert report["pending_entry_orders"] == [
        {
            "order_id": "987",
            "symbol": "ETHUSDT",
            "side": "LONG",
            "status": "NEW",
            "reason": "entry order lacks a validated stop-risk projection",
        }
    ]


@pytest.mark.asyncio
async def test_reduce_only_and_hedge_closing_orders_do_not_count_as_entry_exposure(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    executor.hedge_mode = True
    executor.get_open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 1,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "status": "NEW",
                "reduceOnly": False,
                "closePosition": False,
            },
            {
                "orderId": 2,
                "symbol": "ETHUSDT",
                "side": "BUY",
                "positionSide": "SHORT",
                "status": "NEW",
                "reduceOnly": False,
                "closePosition": False,
            },
            {
                "orderId": 3,
                "symbol": "SOLUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "status": "NEW",
                "reduceOnly": True,
                "closePosition": False,
            },
        ]
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[]
    )

    assert report["account_exposure_verified"] is True
    assert report["pending_entry_orders"] == []


@pytest.mark.asyncio
async def test_closed_trade_protection_history_does_not_require_resting_order(
    protected_intent,
):
    protected_intent.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    protected_intent.mark_applied("aitos-protection-1", "closed-trade-1")
    executor = ReadOnlyExecutor("paper-account", [])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[]
    )

    assert report["findings"] == []
    assert report["account_exposure_verified"] is True


@pytest.mark.asyncio
async def test_closed_trade_with_orphaned_resting_protection_is_reported(
    protected_intent,
):
    protected_intent.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    protected_intent.mark_applied("aitos-protection-1", "closed-trade-1")
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[]
    )

    assert report["findings"][0]["status"] == "unowned_order"
    assert report["account_exposure_verified"] is True


@pytest.mark.asyncio
async def test_live_guard_accepts_only_reconciled_account_and_owned_protection(
    protected_intent,
):
    protected_intent.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    protected_intent.mark_applied("aitos-protection-1", "open-trade-1")
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    trade = SimpleNamespace(
        trade_id="open-trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
    )
    executor.get_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.0",
            }
        ]
    )

    report = await require_live_account_exposure_reconciled(
        executor, protected_intent, [trade], now=NOW
    )

    assert report["account_exposure_verified"] is True


@pytest.mark.asyncio
async def test_live_guard_blocks_unpriced_pending_entry_order(protected_intent):
    executor = ReadOnlyExecutor("paper-account", [])
    executor.get_open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 777,
                "symbol": "ETHUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "status": "NEW",
                "reduceOnly": False,
                "closePosition": False,
            }
        ]
    )

    with pytest.raises(RuntimeError, match="pending_entry_orders=1"):
        await require_live_account_exposure_reconciled(
            executor, protected_intent, [], now=NOW
        )


@pytest.mark.asyncio
async def test_terminal_protection_is_not_expected_in_open_inventory(
    protected_intent,
):
    protected_intent.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    protected_intent.mark_applied("aitos-protection-1", "closed-trade-1")
    protected_intent.mark_protection_terminal(
        "algo:123",
        symbol="BTCUSDT",
        status="CANCELED",
        executed_quantity=0.0,
    )
    executor = ReadOnlyExecutor("paper-account", [])

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[]
    )

    assert report["findings"] == []
    assert report["account_exposure_verified"] is True
    assert protected_intent.protection_intents()[0]["state"] == "TERMINAL"


@pytest.mark.asyncio
async def test_live_account_audit_requires_stop_identity_for_every_open_trade(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [])
    executor.get_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.0",
            }
        ]
    )
    trade = SimpleNamespace(
        trade_id="trade-without-stop",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[trade]
    )

    assert any(
        finding["status"] == "local_position_missing_stop_id"
        for finding in report["findings"]
    )


@pytest.mark.asyncio
async def test_live_account_audit_requires_local_stop_in_authenticated_inventory(
    protected_intent,
):
    executor = ReadOnlyExecutor("paper-account", [])
    executor.get_position_risk = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1.0",
            }
        ]
    )
    trade = SimpleNamespace(
        trade_id="trade-with-missing-stop",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:999",
    )

    report = await audit_exchange_protection_inventory(
        executor, protected_intent, now=NOW, trades=[trade]
    )

    assert any(
        finding["status"] == "local_stop_missing_from_venue_inventory"
        for finding in report["findings"]
    )


@pytest.mark.asyncio
async def test_restart_recovers_accepted_initial_stop_and_applies_after_durable_trade_write(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "recover.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-protection-1",
            reduce_only=True,
            owner_trade_id="trade-1",
        )
    )
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id=None,
        updated_at="",
    )
    persisted = []

    async def persist(value):
        persisted.append((value.trade_id, value.sl_order_id))

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], persist
    )

    assert report["recovered"] == 1
    assert report["blocked"] == []
    assert persisted == [("trade-1", "algo:123")]
    assert trade.sl_order_id == "algo:123"
    assert store.unresolved() == []
    history = store.protection_intents()[0]
    assert history["state"] == "APPLIED"
    assert history["trade_id"] == "trade-1"


@pytest.mark.asyncio
async def test_restart_finishes_journal_ack_after_trade_snapshot_was_already_written(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "recover.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-protection-1",
            reduce_only=True,
            owner_trade_id="trade-1",
        )
    )
    store.record_protection(
        "aitos-protection-1",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="aitos-protection-1",
        ),
    )
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        updated_at="",
    )
    persisted = []

    async def persist(value):
        persisted.append(value.sl_order_id)

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], persist
    )

    assert report["recovered"] == 1
    assert persisted == ["algo:123"]
    assert store.protection_intents()[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_restart_does_not_recover_stop_for_different_existing_protection(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "recover.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-protection-1",
            reduce_only=True,
            owner_trade_id="trade-1",
        )
    )
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:old-stop",
        updated_at="",
    )

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], AsyncMock()
    )

    assert report["recovered"] == 0
    assert "different stop" in report["blocked"][0]["reason"]
    assert store.unresolved()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_restart_recovery_quarantines_legacy_preclaim_without_trade_owner(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "legacy.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-protection-1",
            reduce_only=True,
        )
    )
    executor = ReadOnlyExecutor("paper-account", [inventory_row()])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id=None,
        updated_at="",
    )

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], AsyncMock()
    )

    assert report["recovered"] == 0
    assert "no owner_trade_id" in report["blocked"][0]["reason"]
    assert store.unresolved()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_restart_recovery_rejects_venue_stop_quantity_mismatch(tmp_path):
    store = ExecutionIntentStore(tmp_path / "mismatch.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-protection-1",
            reduce_only=True,
            owner_trade_id="trade-1",
        )
    )
    mismatched = inventory_row()
    mismatched["quantity"] = "0.5"
    executor = ReadOnlyExecutor("paper-account", [mismatched])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id=None,
        updated_at="",
    )

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], AsyncMock()
    )

    assert report["recovered"] == 0
    assert "identity mismatch" in report["blocked"][0]["reason"]
    assert trade.sl_order_id is None
    assert store.unresolved()[0]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_restart_recovers_accepted_initial_take_profit_without_duplicate_order(
    tmp_path,
):
    store = ExecutionIntentStore(tmp_path / "recover-tp.sqlite", scope="paper-account")
    store.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=110.0,
            order_type="TAKE_PROFIT_MARKET",
            client_order_id="aitos-tp-1",
            reduce_only=True,
            owner_trade_id="trade-tp-1",
        )
    )
    venue_tp = inventory_row()
    venue_tp.update(
        algoId=456,
        clientAlgoId="aitos-tp-1",
        orderType="TAKE_PROFIT_MARKET",
        triggerPrice="110.0",
    )
    executor = ReadOnlyExecutor("paper-account", [venue_tp])
    executor.get_algo_order_by_client_id = AsyncMock(return_value=venue_tp)
    trade = SimpleNamespace(
        trade_id="trade-tp-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        updated_at="",
    )
    persisted = []

    async def persist(value):
        persisted.append((value.sl_order_id, list(value.tp_order_ids)))

    report = await recover_accepted_initial_stop_submissions(
        executor, store, [trade], persist
    )

    assert report["recovered"] == 1
    assert report["blocked"] == []
    assert persisted == [("algo:123", ["algo:456"])]
    assert trade.tp_order_ids == ["algo:456"]
    assert store.unresolved() == []
    assert store.protection_intents()[0]["state"] == "APPLIED"


class HandoverExecutor(ReadOnlyExecutor):
    def __init__(self, scope, algo, old_statuses):
        super().__init__(scope, [])
        self.algo = algo
        self.get_algo_order_by_client_id = AsyncMock(return_value=algo)
        self.get_resting_order_status = AsyncMock(side_effect=old_statuses)
        self.cancel_resting_order = AsyncMock()
        self.acknowledge_protection = AsyncMock()
        self.hedge_mode = False


def replacement_algo(client_id="aitos-rpl-0001"):
    row = inventory_row()
    row.update(
        algoId=456,
        clientAlgoId=client_id,
        orderType="STOP_MARKET",
        triggerPrice="95.0",
    )
    return row


def begin_replacement_handover(journal, trade_id="trade-1"):
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=95.0,
            order_type="STOP_MARKET",
            client_order_id="aitos-rpl-0001",
            reduce_only=True,
            owner_trade_id=trade_id,
        )
    )
    journal.begin_protection_handover(
        handover_id="rpl-0001",
        trade_id=trade_id,
        symbol="BTCUSDT",
        old_order_id="algo:123",
        new_client_id="aitos-rpl-0001",
    )


@pytest.mark.asyncio
async def test_restart_recovers_replacement_stop_before_cancelling_old_stop(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    executor = HandoverExecutor(
        "paper-account", replacement_algo(), ["NEW", "CANCELED"]
    )
    executor.acknowledge_protection = lambda result, trade_id: journal.mark_applied(
        result.client_order_id, trade_id
    )
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )
    persisted = []

    async def persist(value):
        persisted.append(
            (
                value.sl_order_id,
                list(value.agent_consensus.get("_pending_stop_cancellations", [])),
            )
        )

    report = await recover_protection_handovers(executor, journal, [trade], persist)

    assert report["recovered"] == 1
    assert report["blocked"] == []
    assert executor.cancel_resting_order.await_count == 1
    assert persisted == [
        ("algo:456", ["algo:123"]),
        ("algo:456", []),
    ]
    assert journal.protection_handovers()[0]["state"] == "APPLIED"
    assert journal.protection_intents()[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_restart_recovers_crash_after_old_stop_was_already_cancelled(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    journal.record_handover_new_order("rpl-0001", "algo:456")
    journal.mark_handover_old_cancel_requested("rpl-0001")
    executor = HandoverExecutor("paper-account", replacement_algo(), ["CANCELED"])
    executor.acknowledge_protection = lambda result, trade_id: journal.mark_applied(
        result.client_order_id, trade_id
    )
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )

    async def persist(_trade):
        return None

    report = await recover_protection_handovers(executor, journal, [trade], persist)

    assert report["recovered"] == 1
    executor.cancel_resting_order.assert_not_awaited()
    assert trade.sl_order_id == "algo:456"
    assert journal.protection_handovers()[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_replacement_recovery_quarantines_old_stop_partial_fill(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    journal.record_handover_new_order("rpl-0001", "algo:456")
    journal.mark_handover_old_cancel_requested("rpl-0001")
    executor = HandoverExecutor(
        "paper-account", replacement_algo(), ["PARTIALLY_FILLED_TERMINAL"]
    )
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )

    report = await recover_protection_handovers(executor, journal, [trade], AsyncMock())

    assert report["recovered"] == 0
    assert "partial" in report["blocked"][0]["reason"]
    executor.cancel_resting_order.assert_not_awaited()
    assert journal.protection_handovers()[0]["state"] == "OLD_CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_replacement_recovery_keeps_old_stop_when_new_algo_is_missing(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    executor = HandoverExecutor("paper-account", None, [])
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )

    report = await recover_protection_handovers(executor, journal, [trade], AsyncMock())

    assert report["recovered"] == 0
    assert trade.sl_order_id == "algo:123"
    executor.cancel_resting_order.assert_not_awaited()
    assert journal.protection_handovers()[0]["state"] == "PREPARED"

@pytest.mark.asyncio
async def test_handover_crash_before_cancel_is_recovered_without_duplicate_post(
    tmp_path,
):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    executor = HandoverExecutor(
        "paper-account", replacement_algo(), ["NEW", "CANCELED"]
    )
    executor.acknowledge_protection = lambda result, trade_id: journal.mark_applied(
        result.client_order_id, trade_id
    )
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )
    calls = 0

    async def fail_first_persist(_trade):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated ClickHouse unavailable")

    first = await recover_protection_handovers(
        executor, journal, [trade], fail_first_persist
    )
    assert first["recovered"] == 0
    assert journal.protection_handovers()[0]["state"] == "NEW_ACCEPTED"
    executor.cancel_resting_order.assert_not_awaited()

    persisted = []

    async def persist(_trade):
        persisted.append(True)

    second = await recover_protection_handovers(
        executor, journal, [trade], persist
    )
    assert second["recovered"] == 1
    assert executor.get_algo_order_by_client_id.await_count == 2
    assert executor.cancel_resting_order.await_count == 1
    assert journal.protection_handovers()[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_handover_crash_after_cancel_ack_replays_without_second_cancel(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "handover.sqlite", scope="paper-account")
    begin_replacement_handover(journal)
    executor = HandoverExecutor(
        "paper-account", replacement_algo(), ["NEW", "CANCELED"]
    )
    executor.acknowledge_protection = lambda result, trade_id: journal.mark_applied(
        result.client_order_id, trade_id
    )
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:123",
        tp_order_ids=[],
        agent_consensus={},
        updated_at="",
    )
    calls = 0

    async def fail_second_persist(_trade):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated ClickHouse write failure after cancel")

    first = await recover_protection_handovers(
        executor, journal, [trade], fail_second_persist
    )
    assert first["recovered"] == 0
    assert journal.protection_handovers()[0]["state"] == "OLD_CANCELLED"
    assert executor.cancel_resting_order.await_count == 1

    second = await recover_protection_handovers(
        executor, journal, [trade], AsyncMock()
    )
    assert second["recovered"] == 1
    assert executor.cancel_resting_order.await_count == 1
    assert journal.protection_handovers()[0]["state"] == "APPLIED"

@pytest.mark.asyncio
async def test_restart_recovers_replacement_take_profit_without_cancelling_early(
    tmp_path,
):
    journal = ExecutionIntentStore(
        tmp_path / "handover-tp.sqlite", scope="paper-account"
    )
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=0.5,
            reference_price=108.0,
            order_type="TAKE_PROFIT_MARKET",
            client_order_id="aitos-tp-old",
            reduce_only=True,
            owner_trade_id="trade-tp",
        )
    )
    journal.record_protection(
        "aitos-tp-old",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            108.0,
            client_order_id="aitos-tp-old",
        ),
    )
    journal.mark_applied("aitos-tp-old", "trade-tp")
    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=0.5,
            reference_price=112.0,
            order_type="TAKE_PROFIT_MARKET",
            client_order_id="aitos-tpr-0001",
            reduce_only=True,
            owner_trade_id="trade-tp",
        )
    )
    journal.begin_protection_handover(
        handover_id="tpr-0001",
        trade_id="trade-tp",
        symbol="BTCUSDT",
        old_order_id="algo:123",
        new_client_id="aitos-tpr-0001",
        order_type="TAKE_PROFIT_MARKET",
    )
    new_algo = replacement_algo("aitos-tpr-0001")
    new_algo.update(
        orderType="TAKE_PROFIT_MARKET",
        triggerPrice="112.0",
        quantity="0.5",
    )
    executor = HandoverExecutor("paper-account", new_algo, ["NEW", "CANCELED"])
    executor.acknowledge_protection = lambda result, trade_id: journal.mark_applied(
        result.client_order_id, trade_id
    )
    trade = SimpleNamespace(
        trade_id="trade-tp",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        sl_order_id="algo:789",
        tp_order_ids=["algo:123", "algo:124"],
        agent_consensus={},
        updated_at="",
    )
    persisted = []

    async def persist(value):
        persisted.append(list(value.tp_order_ids))

    report = await recover_protection_handovers(
        executor, journal, [trade], persist
    )

    assert report["recovered"] == 1
    assert report["blocked"] == []
    assert trade.sl_order_id == "algo:789"
    assert trade.tp_order_ids == ["algo:456", "algo:124"]
    assert persisted == [["algo:456", "algo:124"], ["algo:456", "algo:124"]]
    assert executor.cancel_resting_order.await_count == 1
    assert journal.protection_handovers()[0]["order_type"] == "TAKE_PROFIT_MARKET"
    assert journal.protection_handovers()[0]["state"] == "APPLIED"


def test_protection_handover_rejects_order_type_identity_reuse(tmp_path):
    journal = ExecutionIntentStore(
        tmp_path / "handover-type.sqlite", scope="paper-account"
    )
    journal.begin_protection_handover(
        handover_id="same-id",
        trade_id="trade-1",
        symbol="BTCUSDT",
        old_order_id="algo:123",
        new_client_id="new-stop",
        order_type="STOP_MARKET",
    )
    with pytest.raises(ValueError, match="different identity"):
        journal.begin_protection_handover(
            handover_id="same-id",
            trade_id="trade-1",
            symbol="BTCUSDT",
            old_order_id="algo:123",
            new_client_id="new-stop",
            order_type="TAKE_PROFIT_MARKET",
        )
