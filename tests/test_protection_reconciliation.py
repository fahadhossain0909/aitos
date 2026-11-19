from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from aitos.execution.intent_store import ExecutionIntentStore
from aitos.execution.order_executor import OrderRequest
from aitos.execution.protection_reconciliation import (
    audit_exchange_protection_inventory,
)
from aitos.models.trade import TradeSide

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


class ReadOnlyExecutor:
    def __init__(self, scope, orders):
        self.execution_scope = scope
        self.get_open_algo_orders = AsyncMock(return_value=orders)


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
