"""Real local SQLite durability and offline executor wiring; no account/network."""

import asyncio
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
    journal.begin(request(order_type="STOP_MARKET", reduce_only=True))
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
            "intent-one", OrderResult("123", "BTCUSDT", TradeSide.LONG, 0, 90)
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
    with pytest.raises(TimeoutError):
        await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)
    assert store(tmp_path).unresolved()[0]["state"] == "SUBMITTING"
    with pytest.raises(ExecutionUncertainError):
        await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 90)
    assert value._signed_request.await_count == 1


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
