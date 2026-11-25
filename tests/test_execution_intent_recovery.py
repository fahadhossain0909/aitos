"""Real local SQLite durability and offline executor wiring; no account/network."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.execution.intent_store import (
    ExecutionIntentStore,
    ExecutionRecoveryRequired,
    ExecutionUncertainError,
)
from aitos.execution.order_executor import OrderRequest, OrderResult
from aitos.models.trade import TradeSide


def request(**kw):
    values = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        reference_price=100.0,
        client_order_id="intent-one",
    )
    values.update(kw)
    return OrderRequest(**values)


def result(success=True):
    return OrderResult(
        "12",
        "BTCUSDT",
        TradeSide.LONG,
        1 if success else 0,
        101 if success else 0,
        success=success,
    )


def store(tmp_path, scope="testnet-account-fingerprint"):
    return ExecutionIntentStore(tmp_path / "intents.sqlite3", scope=scope)


def test_pre_submit_intent_survives_reopen_and_blocks_startup(tmp_path):
    store(tmp_path).begin(request())
    reopened = store(tmp_path)
    assert reopened.unresolved()[0]["state"] == "SUBMITTING"
    with pytest.raises(ExecutionRecoveryRequired):
        reopened.assert_ready()
    with pytest.raises(ExecutionRecoveryRequired):
        reopened.begin(request())


def test_payload_conflict_is_not_a_replay(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    with pytest.raises(ValueError, match="different durable request"):
        store(tmp_path).begin(request(quantity=2))


def test_confirmed_fill_stays_quarantined_until_lifecycle_applies(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    journal.record_result("intent-one", result())
    assert store(tmp_path).unresolved()[0]["state"] == "CONFIRMED"
    with pytest.raises(ExecutionRecoveryRequired):
        journal.begin(request(client_order_id="second-entry", symbol="ETHUSDT"))
    journal.mark_applied("intent-one", "durable-trade-one")
    journal.assert_ready()
    with pytest.raises(ExecutionRecoveryRequired):
        journal.begin(request())


def test_unknown_entry_does_not_block_reduce_only_de_risking(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    journal.begin(request(client_order_id="exit-one", reduce_only=True))
    assert len(journal.unresolved()) == 2


def test_account_and_environment_scopes_are_isolated(tmp_path):
    store(tmp_path, "account-A-testnet").begin(request())
    other = store(tmp_path, "account-A-mainnet")
    other.assert_ready()
    other.begin(request())
    assert len(other.unresolved()) == 1


def test_rejected_order_does_not_block_new_intent(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    journal.record_result("intent-one", result(False))
    journal.assert_ready()
    journal.begin(request(client_order_id="second"))


@pytest.mark.parametrize(
    "action", ["apply_missing", "apply_unconfirmed", "record_missing", "apply_empty_id"]
)
def test_invalid_state_transitions_are_rejected(tmp_path, action):
    journal = store(tmp_path)
    with pytest.raises(ValueError):
        if action == "apply_missing":
            journal.mark_applied("missing", "trade")
        elif action == "apply_unconfirmed":
            journal.begin(request())
            journal.mark_applied("intent-one", "trade")
        elif action == "record_missing":
            journal.record_result("missing", result())
        else:
            journal.mark_applied("intent-one", "")


async def test_journal_claim_precedes_venue_and_acknowledgement_follows_apply(tmp_path):
    journal = store(tmp_path)
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)

    async def submit(*args):
        assert store(tmp_path).unresolved()[0]["state"] == "SUBMITTING"
        return dict(orderId=12, status="FILLED", executedQty="1", avgPrice="101")

    value._signed_request = AsyncMock(side_effect=submit)
    assert (await value.submit_order(request())).success
    assert value.has_unresolved_orders("BTCUSDT")
    value.acknowledge_execution("intent-one", "trade-one")
    assert not value.has_unresolved_orders()


async def test_pre_submit_journal_failure_prevents_all_venue_calls(
    tmp_path, monkeypatch
):
    journal = store(tmp_path)
    monkeypatch.setattr(
        journal, "begin", lambda req: (_ for _ in ()).throw(OSError("disk failure"))
    )
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)
    value._signed_request = AsyncMock()
    with pytest.raises(OSError):
        await value.submit_order(request())
    value._signed_request.assert_not_called()


async def test_post_fill_journal_failure_keeps_submitting_evidence(
    tmp_path, monkeypatch
):
    journal = store(tmp_path)
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)
    value._signed_request = AsyncMock(
        return_value=dict(orderId=12, status="FILLED", executedQty="1", avgPrice="101")
    )
    monkeypatch.setattr(
        journal,
        "record_result",
        lambda *args: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(ExecutionUncertainError):
        await value.submit_order(request())
    assert store(tmp_path).unresolved()[0]["state"] == "SUBMITTING"


async def test_cancellation_keeps_intent_across_restart(tmp_path):
    journal = store(tmp_path)
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)
    started = asyncio.Event()

    async def submit(*args):
        started.set()
        await asyncio.Event().wait()

    value._signed_request = AsyncMock(side_effect=submit)
    task = asyncio.create_task(value.submit_order(request()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ExecutionRecoveryRequired):
        store(tmp_path).assert_ready()


@pytest.mark.parametrize(
    "quantity,price,success",
    [(0, 100, True), (1, 0, True), (float("nan"), 100, True), (1, 100, False)],
)
def test_invalid_terminal_result_cannot_release_quarantine(
    tmp_path, quantity, price, success
):
    journal = store(tmp_path)
    journal.begin(request())
    bad = OrderResult("12", "BTCUSDT", TradeSide.LONG, quantity, price, success=success)
    with pytest.raises(ValueError):
        journal.record_result("intent-one", bad)
    assert journal.unresolved()[0]["state"] == "SUBMITTING"


def test_offline_inventory_never_equates_empty_history_with_verified_account(tmp_path):
    from aitos.execution.recovery import read_recovery_inventory

    journal = store(tmp_path)
    report = read_recovery_inventory(journal.path, "typo-scope")
    assert not report["scope_has_history"]
    assert not report["venue_exposure_verified"]
    journal.begin(request())
    report = read_recovery_inventory(journal.path, journal.scope)
    assert report["recovery_required"]
    assert report["unresolved_intents"][0]["client_id"] == "intent-one"
    assert journal.unresolved()[0]["state"] == "SUBMITTING"


def test_read_only_inventory_does_not_create_missing_database(tmp_path):
    import sqlite3

    from aitos.execution.recovery import read_recovery_inventory

    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        read_recovery_inventory(path, "scope")
    assert not path.exists()


def test_protection_acceptance_survives_crash_until_durable_id_applied(tmp_path):
    journal = store(tmp_path)
    journal.begin(
        request(order_type="STOP_MARKET", reduce_only=True, reference_price=90)
    )
    accepted = OrderResult(
        "algo:123", "BTCUSDT", TradeSide.LONG, 0, 90, client_order_id="intent-one"
    )
    journal.record_protection("intent-one", accepted)
    reopened = store(tmp_path)
    assert reopened.unresolved()[0]["state"] == "PROTECTED"
    with pytest.raises(ExecutionRecoveryRequired):
        reopened.assert_ready()
    reopened.mark_applied("intent-one", "durable-trade")
    store(tmp_path).assert_ready()


@pytest.mark.parametrize(
    "order_type,reduce_only", [("MARKET", True), ("STOP_MARKET", False)]
)
def test_protection_record_rejects_nonprotective_preclaims(
    tmp_path, order_type, reduce_only
):
    journal = store(tmp_path)
    journal.begin(request(order_type=order_type, reduce_only=reduce_only))
    with pytest.raises(ValueError, match="protective preclaim"):
        journal.record_protection(
            "intent-one",
            OrderResult(
                "123",
                "BTCUSDT",
                TradeSide.LONG,
                0,
                90,
                client_order_id="intent-one",
            ),
        )
    assert journal.unresolved()[0]["state"] == "SUBMITTING"


def test_protection_acceptance_cannot_be_treated_as_fill(tmp_path):
    journal = store(tmp_path)
    journal.begin(request(order_type="TAKE_PROFIT_MARKET", reduce_only=True))
    with pytest.raises(ValueError, match="unfilled resting order"):
        journal.record_protection("intent-one", result())


async def test_protective_post_is_preclaimed_and_uncertain_post_stays_blocked(tmp_path):
    journal = store(tmp_path)
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)

    async def uncertain_post(method, path, params):
        assert journal.unresolved()[0]["state"] == "SUBMITTING"
        assert journal.unresolved()[0]["client_id"] == params["clientAlgoId"]
        raise TimeoutError("offline response lost")

    value._signed_request = AsyncMock(side_effect=uncertain_post)
    with pytest.raises(ExecutionUncertainError, match="outcome and client-ID recovery lookup"):
        await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)
    assert store(tmp_path).unresolved()[0]["state"] == "SUBMITTING"
    with pytest.raises(ExecutionUncertainError):
        await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)
    assert value._signed_request.await_count == 2


async def test_protection_acknowledgement_clears_only_after_durable_application(
    tmp_path,
):
    journal = store(tmp_path)
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)
    value._signed_request = AsyncMock(return_value={"algoId": 321, "algoStatus": "NEW"})
    accepted = await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)
    assert journal.unresolved()[0]["state"] == "PROTECTED"
    value.acknowledge_protection(accepted, "durable-trade")
    journal.assert_ready()


@pytest.mark.parametrize(
    "response", [{"status": "NEW"}, {"orderId": 123, "status": "FILLED"}]
)
async def test_legacy_protection_never_fabricates_resting_acceptance(response):
    value = BinanceFuturesOrderExecutor(
        "dummy", "dummy", conditional_orders_api="legacy"
    )
    value._signed_request = AsyncMock(return_value=response)
    with pytest.raises(ExecutionUncertainError):
        await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)


@pytest.mark.parametrize(
    "result",
    [
        OrderResult(
            "algo:123",
            "ETHUSDT",
            TradeSide.LONG,
            0,
            90,
            client_order_id="intent-one",
        ),
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.SHORT,
            0,
            90,
            client_order_id="intent-one",
        ),
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0,
            90,
            client_order_id="other",
        ),
        OrderResult(
            "algo:0",
            "BTCUSDT",
            TradeSide.LONG,
            0,
            90,
            client_order_id="intent-one",
        ),
        OrderResult(
            "algo:not-an-id",
            "BTCUSDT",
            TradeSide.LONG,
            0,
            90,
            client_order_id="intent-one",
        ),
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0,
            float("nan"),
            client_order_id="intent-one",
        ),
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0,
            91,
            client_order_id="intent-one",
        ),
    ],
)
def test_protection_acceptance_must_match_preclaim_identity(tmp_path, result):
    journal = store(tmp_path)
    journal.begin(request(order_type="STOP_MARKET", reduce_only=True))
    with pytest.raises(ValueError):
        journal.record_protection("intent-one", result)
    assert journal.unresolved()[0]["state"] == "SUBMITTING"


def test_protection_acceptance_rejects_nonfinite_quantity(tmp_path):
    journal = store(tmp_path)
    journal.begin(request(order_type="STOP_MARKET", reduce_only=True))
    accepted = OrderResult(
        "algo:123",
        "BTCUSDT",
        TradeSide.LONG,
        float("nan"),
        90,
        client_order_id="intent-one",
    )
    with pytest.raises(ValueError, match="valid identity"):
        journal.record_protection("intent-one", accepted)
    assert journal.unresolved()[0]["state"] == "SUBMITTING"


def test_protection_acceptance_rejects_missing_or_already_transitioned_preclaim(
    tmp_path,
):
    journal = store(tmp_path)
    accepted = OrderResult(
        "algo:123",
        "BTCUSDT",
        TradeSide.LONG,
        0,
        90,
        client_order_id="intent-one",
    )
    missing = OrderResult(
        "algo:123",
        "BTCUSDT",
        TradeSide.LONG,
        0,
        90,
        client_order_id="missing",
    )
    with pytest.raises(ValueError, match="no submitting preclaim"):
        journal.record_protection("missing", missing)
    journal.begin(
        request(order_type="STOP_MARKET", reduce_only=True, reference_price=90)
    )
    journal.record_protection("intent-one", accepted)
    with pytest.raises(ValueError, match="no submitting preclaim"):
        journal.record_protection("intent-one", accepted)
    assert journal.unresolved()[0]["state"] == "PROTECTED"


def test_confirmed_terminal_protection_is_durable_and_no_longer_unresolved(tmp_path):
    journal = store(tmp_path)
    journal.begin(
        request(
            order_type="STOP_MARKET",
            reduce_only=True,
            reference_price=90.0,
        )
    )
    journal.record_protection(
        "intent-one",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="intent-one",
        ),
    )
    journal.mark_applied("intent-one", "trade-one")

    assert journal.mark_protection_terminal(
        "algo:123",
        symbol="BTCUSDT",
        status="CANCELED",
        executed_quantity=0.25,
    )

    assert journal.unresolved() == []
    journal.assert_ready()
    history = journal.protection_intents()
    assert history[0]["state"] == "TERMINAL"
    terminal = json.loads(history[0]["result_json"])
    assert terminal["terminal_status"] == "CANCELED"
    assert terminal["terminal_executed_quantity"] == 0.25
    assert not journal.mark_protection_terminal(
        "algo:123",
        symbol="BTCUSDT",
        status="CANCELED",
        executed_quantity=0.25,
    )

    journal.begin(request(client_order_id="new-entry", symbol="ETHUSDT"))
    assert journal.unresolved()[0]["client_id"] == "new-entry"


def test_terminal_protection_does_not_release_unrelated_intent(tmp_path):
    journal = store(tmp_path)
    journal.begin(
        request(
            order_type="STOP_MARKET",
            reduce_only=True,
            reference_price=90.0,
        )
    )
    journal.record_protection(
        "intent-one",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="intent-one",
        ),
    )
    journal.mark_applied("intent-one", "trade-one")
    assert not journal.mark_protection_terminal(
        "algo:999",
        symbol="BTCUSDT",
        status="CANCELED",
    )
    assert journal.protection_intents()[0]["state"] == "APPLIED"


@pytest.mark.parametrize("status", ["NEW", "PARTIALLY_FILLED", "UNKNOWN"])
def test_nonterminal_protection_cannot_be_marked_terminal(tmp_path, status):
    journal = store(tmp_path)
    with pytest.raises(ValueError):
        journal.mark_protection_terminal("algo:123", symbol="BTCUSDT", status=status)


async def test_confirmed_binance_algo_cancel_updates_protection_journal(tmp_path):
    journal = store(tmp_path)
    journal.begin(
        request(
            order_type="STOP_MARKET",
            reduce_only=True,
            reference_price=90.0,
        )
    )
    journal.record_protection(
        "intent-one",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="intent-one",
        ),
    )
    journal.mark_applied("intent-one", "trade-one")
    executor = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=journal)
    executor._signed_request = AsyncMock(
        side_effect=[
            {"algoId": 123, "algoStatus": "CANCELED"},
            {
                "algoId": 123,
                "symbol": "BTCUSDT",
                "algoStatus": "CANCELED",
            },
        ]
    )

    result = await executor.cancel_order("BTCUSDT", "algo:123")

    assert result["status"] == "CANCELED"
    assert journal.unresolved() == []
    assert journal.protection_intents()[0]["state"] == "TERMINAL"


def test_protection_handover_requires_new_order_before_old_cancel(tmp_path):
    journal = store(tmp_path)
    prepared = journal.begin_protection_handover(
        handover_id="replace-stop-1",
        trade_id="trade-1",
        symbol="BTCUSDT",
        old_order_id="algo:100",
        new_client_id="aitos-rpl-0001",
    )
    assert prepared["state"] == "PREPARED"
    with pytest.raises(ValueError, match="accepted replacement"):
        journal.mark_handover_old_cancel_requested("replace-stop-1")

    journal.record_handover_new_order("replace-stop-1", "algo:200")
    journal.mark_handover_old_cancel_requested("replace-stop-1")
    assert journal.protection_handovers()[0]["state"] == "OLD_CANCEL_REQUESTED"
    journal.mark_handover_old_cancelled("replace-stop-1")
    journal.complete_protection_handover("replace-stop-1")
    assert store(tmp_path).protection_handovers()[0]["state"] == "APPLIED"


def test_protection_handover_restart_is_idempotent_and_identity_bound(tmp_path):
    journal = store(tmp_path)
    journal.begin_protection_handover(
        handover_id="replace-stop-2",
        trade_id="trade-1",
        symbol="BTCUSDT",
        old_order_id="algo:100",
        new_client_id="aitos-rpl-0002",
    )
    journal.record_handover_new_order("replace-stop-2", "algo:201")
    reopened = store(tmp_path)
    row = reopened.protection_handovers()[0]
    assert row["state"] == "NEW_ACCEPTED"
    assert row["new_order_id"] == "algo:201"

    replay = reopened.begin_protection_handover(
        handover_id="replace-stop-2",
        trade_id="trade-1",
        symbol="BTCUSDT",
        old_order_id="algo:100",
        new_client_id="aitos-rpl-0002",
    )
    assert replay["state"] == "NEW_ACCEPTED"
    with pytest.raises(ValueError, match="different identity"):
        reopened.begin_protection_handover(
            handover_id="replace-stop-2",
            trade_id="trade-1",
            symbol="BTCUSDT",
            old_order_id="algo:999",
            new_client_id="aitos-rpl-0002",
        )


def test_second_active_handover_for_trade_is_blocked(tmp_path):
    journal = store(tmp_path)
    journal.begin_protection_handover(
        handover_id="replace-stop-3",
        trade_id="trade-1",
        symbol="BTCUSDT",
        old_order_id="algo:100",
        new_client_id="aitos-rpl-0003",
    )
    with pytest.raises(ExecutionRecoveryRequired, match="another protective handover"):
        journal.begin_protection_handover(
            handover_id="replace-stop-4",
            trade_id="trade-1",
            symbol="BTCUSDT",
            old_order_id="algo:100",
            new_client_id="aitos-rpl-0004",
        )


def test_existing_protection_handover_database_migrates_order_type(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-handover.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE protection_handovers (
                scope TEXT NOT NULL,
                handover_id TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                old_order_id TEXT NOT NULL,
                new_client_id TEXT NOT NULL,
                new_order_id TEXT,
                state TEXT NOT NULL,
                blocked_reason TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(scope, handover_id),
                UNIQUE(scope, new_client_id)
            )"""
        )
        conn.execute(
            "INSERT INTO protection_handovers(scope,handover_id,trade_id,symbol,"
            "old_order_id,new_client_id,state) VALUES (?,?,?,?,?,?,?)",
            ("scope", "legacy", "trade", "BTCUSDT", "algo:1", "new-stop", "PREPARED"),
        )

    journal = ExecutionIntentStore(path, scope="scope")
    rows = journal.protection_handovers()
    assert rows[0]["order_type"] == "STOP_MARKET"
    journal.begin_protection_handover(
        handover_id="tp-new",
        trade_id="trade-2",
        symbol="BTCUSDT",
        old_order_id="algo:2",
        new_client_id="new-tp",
        order_type="TAKE_PROFIT_MARKET",
    )
    assert journal.protection_handovers(trade_id="trade-2")[0]["order_type"] == (
        "TAKE_PROFIT_MARKET"
    )


def test_protection_child_watermark_is_monotonic_and_identity_scoped(tmp_path):
    journal = store(tmp_path)
    first = journal.mark_protection_child_applied(
        algo_id="123",
        child_order_id="456",
        trade_id="trade-one",
        symbol="BTCUSDT",
        cumulative_quantity="0.25",
        cumulative_quote="22.5",
        status="PARTIALLY_FILLED",
    )
    assert first["cumulative_quantity"] == "0.25"
    replay = journal.mark_protection_child_applied(
        algo_id="123",
        child_order_id="456",
        trade_id="trade-one",
        symbol="BTCUSDT",
        cumulative_quantity="0.25",
        cumulative_quote="22.5",
        status="CANCELED",
    )
    assert replay["status"] == "CANCELED"
    advanced = journal.mark_protection_child_applied(
        algo_id="123",
        child_order_id="456",
        trade_id="trade-one",
        symbol="BTCUSDT",
        cumulative_quantity="0.5",
        cumulative_quote="45",
        status="FILLED",
    )
    assert advanced["cumulative_quantity"] == "0.5"
    with pytest.raises(ValueError, match="regressed"):
        journal.mark_protection_child_applied(
            algo_id="123",
            child_order_id="456",
            trade_id="trade-one",
            symbol="BTCUSDT",
            cumulative_quantity="0.4",
            cumulative_quote="36",
            status="FILLED",
        )
    with pytest.raises(ValueError, match="reused across trade scope"):
        journal.mark_protection_child_applied(
            algo_id="123",
            child_order_id="456",
            trade_id="trade-two",
            symbol="BTCUSDT",
            cumulative_quantity="0.5",
            cumulative_quote="45",
            status="FILLED",
        )


def test_execution_transitions_are_append_only_and_use_transaction_sequence(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    journal.record_result("intent-one", result())
    journal.mark_applied("intent-one", "durable-trade-one")

    first_page = journal.read_transition_events(limit=2)
    second_page = journal.read_transition_events(
        after_sequence=first_page[-1]["sequence"], limit=2
    )
    assert [event["event_type"] for event in first_page] == [
        "INTENT_CREATED",
        "INTENT_STATE_CHANGED",
    ]
    assert [event["event_type"] for event in second_page] == ["INTENT_STATE_CHANGED"]
    assert [event["sequence"] for event in first_page + second_page] == sorted(
        event["sequence"] for event in first_page + second_page
    )
    assert second_page[0]["before_json"]
    assert '"state":"CONFIRMED"' in second_page[0]["before_json"]
    assert '"state":"APPLIED"' in second_page[0]["after_json"]

    import sqlite3

    with sqlite3.connect(journal.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE execution_transition_events SET event_type='ERASED' "
                "WHERE sequence=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM execution_transition_events WHERE sequence=1")


def test_execution_transition_sequence_is_scoped_and_survives_reopen(tmp_path):
    journal = store(tmp_path, scope="account-A")
    journal.begin(request())
    last = journal.read_transition_events()[-1]["sequence"]

    other = store(tmp_path, scope="account-B")
    assert other.read_transition_events() == []
    other.begin(request(client_order_id="other-scope"))
    assert other.read_transition_events()[0]["sequence"] > 0

    reopened = store(tmp_path, scope="account-A")
    assert reopened.read_transition_events()[0]["sequence"] == last


def test_repeated_applied_acknowledgement_is_idempotent_and_trade_scoped(tmp_path):
    journal = store(tmp_path)
    journal.begin(request())
    journal.record_result("intent-one", result())
    journal.mark_applied("intent-one", "durable-trade-one")

    # Cumulative protective child fills can re-enter the acknowledgement path.
    journal.mark_applied("intent-one", "durable-trade-one")
    assert journal.get("intent-one")["state"] == "APPLIED"
    assert journal.get("intent-one")["trade_id"] == "durable-trade-one"

    with pytest.raises(ValueError, match="different trade"):
        journal.mark_applied("intent-one", "different-trade")
