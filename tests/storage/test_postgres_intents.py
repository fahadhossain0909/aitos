import os
import uuid

import pytest

from aitos.execution.postgres_intents import (
    PostgresCanonicalExecutionIntentStore,
    _financial_state_snapshot,
)
from aitos.execution.postgres_oms import OMSConflict, PostgresCanonicalOMS
from aitos.storage.postgres_migrations import apply_postgres_migrations


def test_postgres_execution_intent_transition_graph_is_fail_closed():
    from aitos.execution.postgres_intents import _ALLOWED_TRANSITIONS

    assert _ALLOWED_TRANSITIONS["SUBMITTING"] == {"CONFIRMED", "REJECTED", "PROTECTED"}
    assert _ALLOWED_TRANSITIONS["REJECTED"] == set()
    assert _ALLOWED_TRANSITIONS["TERMINAL"] == set()
    assert "SUBMITTING" not in _ALLOWED_TRANSITIONS["APPLIED"]


@pytest.mark.asyncio
async def test_postgres_execution_intent_is_atomic_fenced_and_recoverable():
    dsn = os.getenv("AITOS_POSTGRES_OMS_TEST_DSN")
    if not dsn:
        pytest.skip("AITOS_POSTGRES_OMS_TEST_DSN is not configured")
    import asyncpg

    schema = "aitos_intent_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    try:
        assert await apply_postgres_migrations(pool) == 7
        oms = PostgresCanonicalOMS(pool)
        fence = await oms.acquire_writer(
            "paper:intent-test", "worker-a", expected_generation=0
        )
        intents = PostgresCanonicalExecutionIntentStore(oms, fence)
        created = await intents.begin(
            {
                "client_order_id": "client-001",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "0.01",
                "reduce_only": False,
            }
        )
        assert created["state"] == "SUBMITTING"
        assert created["revision"] == 1

        with pytest.raises(OMSConflict, match="already exists"):
            await intents.begin(
                {
                    "client_order_id": "client-001",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "quantity": "0.01",
                    "reduce_only": False,
                }
            )

        with pytest.raises(ValueError, match="positive fill evidence"):
            await intents.transition(
                "client-001",
                expected_revision=1,
                expected_state="SUBMITTING",
                new_state="CONFIRMED",
                result={
                    "order_id": "9001",
                    "filled_quantity": "0",
                    "fill_price": "65000",
                },
            )

        confirmed = await intents.transition(
            "client-001",
            expected_revision=1,
            expected_state="SUBMITTING",
            new_state="CONFIRMED",
            result={
                "order_id": "9001",
                "filled_quantity": "0.01",
                "fill_price": "65000",
            },
        )
        assert confirmed["state"] == "CONFIRMED"
        assert confirmed["revision"] == 2
        with pytest.raises(OMSConflict, match="revision CAS failed"):
            await intents.transition_with_trade_snapshot(
                "client-001",
                expected_revision=2,
                expected_state="CONFIRMED",
                trade_id="trade-atomic-failure",
                expected_trade_revision=5,
                trade_snapshot={
                    "trade_id": "trade-atomic-failure",
                    "account_scope": "paper:intent-test",
                    "symbol": "BTCUSDT",
                    "state": "POSITION_OPENED",
                },
                ledger_entries=[
                    {
                        "account_code": "OPEN_POSITION_ASSET",
                        "currency": "USDT",
                        "debit": "650",
                        "credit": "0",
                    },
                    {
                        "account_code": "CASH_CLEARING",
                        "currency": "USDT",
                        "debit": "0",
                        "credit": "650",
                    },
                ],
            )
        unchanged = await intents.get("client-001")
        assert unchanged["state"] == "CONFIRMED"
        assert unchanged["revision"] == 2
        assert await oms.read_events("paper:intent-test", "trade-atomic-failure") == []

        applied = await intents.transition_with_trade_snapshot(
            "client-001",
            expected_revision=2,
            expected_state="CONFIRMED",
            trade_id="trade-001",
            expected_trade_revision=0,
            trade_snapshot={
                "trade_id": "trade-001",
                "account_scope": "paper:intent-test",
                "symbol": "BTCUSDT",
                "state": "POSITION_OPENED",
                "quantity": "0.01",
                "entry_price": "65000",
            },
            ledger_entries=[
                {
                    "account_code": "OPEN_POSITION_ASSET",
                    "currency": "USDT",
                    "debit": "650",
                    "credit": "0",
                    "reference": "entry:client-001",
                },
                {
                    "account_code": "CASH_CLEARING",
                    "currency": "USDT",
                    "debit": "0",
                    "credit": "650",
                    "reference": "entry:client-001",
                },
            ],
            financial_snapshot={
                "currency": "USDT",
                "starting_equity": "1000",
                "realized_pnl": "-20",
                "unrealized_pnl": "-20",
                "fees": "10",
                "funding": "0",
            },
            expected_financial_revision=0,
        )
        assert applied["trade_id"] == "trade-001"
        assert applied["state"] == "APPLIED"
        assert applied["revision"] == 3
        assert applied["trade_revision"] == 1
        assert await intents.unresolved() == []
        assert await intents.get("client-001") is not None

        events = await oms.read_events("paper:intent-test", "intent-client-001")
        assert [event.revision for event in events] == [1, 2, 3]
        assert [event.payload["event_type"] for event in events] == [
            "EXECUTION_INTENT_CREATED",
            "EXECUTION_INTENT_TRANSITIONED",
            "EXECUTION_INTENT_TRANSITIONED",
        ]
        trade_events = await oms.read_events("paper:intent-test", "trade-001")
        assert [event.revision for event in trade_events] == [1]
        assert (
            trade_events[0].payload["causality"]["causation_id"]
            == applied["last_event_id"]
        )
        ledger = await oms.financial_trade_ledger("paper:intent-test", "trade-001")
        assert len(ledger) == 2
        assert (
            sum(item["debit"] for item in ledger)
            == sum(item["credit"] for item in ledger)
            == 650
        )
        financial_events = await oms.read_events("paper:intent-test", "financial:USDT")
        assert len(financial_events) == 1
        assert financial_events[0].revision == 1
        assert financial_events[0].payload["event_type"] == "FINANCIAL_STATE_SNAPSHOT"
        financial_state = await intents.latest_financial_state("USDT")
        assert financial_state == {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "-20",
            "unrealized_pnl": "-20",
            "fees": "10",
            "funding": "0",
            "venue_equity": "950",
            "unattributed_adjustments": "0",
            "equity": "950",
            "peak_equity": "1000",
            "drawdown": "50",
            "drawdown_pct": "5",
            "max_drawdown": "50",
            "max_drawdown_pct": "5",
        }
        assert applied["financial_event_id"] == financial_events[0].event_id
        assert applied["financial_revision"] == 1
        assert applied["financial_state"] == financial_state
        assert len(await oms.claim_outbox()) == 5

        stale_fence = fence
        next_fence = await oms.acquire_writer(
            "paper:intent-test", "worker-b", expected_generation=1
        )
        stale_store = PostgresCanonicalExecutionIntentStore(oms, stale_fence)
        with pytest.raises(OMSConflict, match="stale writer"):
            await stale_store.transition(
                "client-001",
                expected_revision=3,
                expected_state="APPLIED",
                new_state="TERMINAL",
            )
        fresh_store = PostgresCanonicalExecutionIntentStore(oms, next_fence)
        with pytest.raises(OMSConflict, match="revision/state CAS"):
            await fresh_store.transition(
                "client-001",
                expected_revision=2,
                expected_state="CONFIRMED",
                new_state="APPLIED",
                trade_id="trade-001",
            )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_protection_handover_and_child_fill_journal_are_fenced():
    dsn = os.getenv("AITOS_POSTGRES_OMS_TEST_DSN")
    if not dsn:
        pytest.skip("AITOS_POSTGRES_OMS_TEST_DSN is not configured")
    import asyncpg

    schema = "aitos_protection_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    try:
        assert await apply_postgres_migrations(pool) == 7
        oms = PostgresCanonicalOMS(pool)
        fence = await oms.acquire_writer(
            "live:protection-test", "protection-worker", expected_generation=0
        )
        intents = PostgresCanonicalExecutionIntentStore(oms, fence)

        created = await intents.begin_protection_handover(
            handover_id="rpl-001",
            trade_id="trade-protection-1",
            symbol="BTCUSDT",
            old_order_id="algo:8001",
            new_client_id="aitos-rpl-001",
        )
        assert created["state"] == "PREPARED"
        accepted = await intents.record_handover_new_order("rpl-001", "algo:9001")
        assert accepted["state"] == "NEW_ACCEPTED"
        requested = await intents.mark_handover_old_cancel_requested("rpl-001")
        assert requested["state"] == "OLD_CANCEL_REQUESTED"
        cancelled = await intents.mark_handover_old_cancelled("rpl-001")
        assert cancelled["state"] == "OLD_CANCELLED"
        completed = await intents.complete_protection_handover("rpl-001")
        assert completed["state"] == "APPLIED"
        handovers = await intents.protection_handovers(
            trade_id="trade-protection-1"
        )
        assert len(handovers) == 1
        assert handovers[0]["new_order_id"] == "algo:9001"

        first = await intents.mark_protection_child_applied(
            algo_id="9001",
            child_order_id="91001",
            trade_id="trade-protection-1",
            symbol="BTCUSDT",
            cumulative_quantity="0.005",
            cumulative_quote="325",
            status="PARTIALLY_FILLED",
        )
        assert str(first["cumulative_quantity"]) == "0.005"
        second = await intents.mark_protection_child_applied(
            algo_id="9001",
            child_order_id="91001",
            trade_id="trade-protection-1",
            symbol="BTCUSDT",
            cumulative_quantity="0.01",
            cumulative_quote="650",
            status="FILLED",
        )
        assert str(second["cumulative_quantity"]) == "0.01"
        watermark = await intents.protection_child_watermark("9001", "91001")
        assert str(watermark["cumulative_quote"]) == "650"
        with pytest.raises(OMSConflict, match="regressed"):
            await intents.mark_protection_child_applied(
                algo_id="9001",
                child_order_id="91001",
                trade_id="trade-protection-1",
                symbol="BTCUSDT",
                cumulative_quantity="0.009",
                cumulative_quote="600",
                status="PARTIALLY_FILLED",
            )

        handover_events = await oms.read_events(
            "live:protection-test", "protection-handover-rpl-001"
        )
        child_events = await oms.read_events(
            "live:protection-test", "protection-child-9001-91001"
        )
        assert [event.revision for event in handover_events] == [1, 2, 3, 4, 5]
        assert [event.revision for event in child_events] == [1, 2]
        assert handover_events[-1].payload["event_type"] == (
            "PROTECTION_HANDOVER_COMPLETED"
        )
        assert child_events[-1].payload["cumulative_quantity"] == "0.01"
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_financial_state_snapshot_reconciles_venue_equity_without_hiding_variance():
    first = _financial_state_snapshot(
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "20",
            "unrealized_pnl": "5",
            "fees": "2",
            "funding": "1",
            "venue_equity": "1020",
        },
        None,
    )
    second = _financial_state_snapshot(
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "20",
            "unrealized_pnl": "-30",
            "fees": "2",
            "funding": "1",
            "venue_equity": "990",
        },
        first,
    )

    assert first["equity"] == "1020"
    assert first["venue_equity"] == "1020"
    assert first["unattributed_adjustments"] == "-4"
    assert first["peak_equity"] == "1020"
    assert second["equity"] == "990"
    assert second["unattributed_adjustments"] == "1"
    assert second["drawdown"] == "30"
    assert 2.94 < float(second["drawdown_pct"]) < 2.95
    assert second["max_drawdown"] == "30"


def test_financial_state_snapshot_keeps_peak_and_max_drawdown_monotonic():
    first = _financial_state_snapshot(
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "-20",
            "unrealized_pnl": "-20",
            "fees": "10",
            "funding": "0",
        },
        None,
    )
    second = _financial_state_snapshot(
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "-20",
            "unrealized_pnl": "-80",
            "fees": "10",
            "funding": "0",
        },
        first,
    )
    recovered = _financial_state_snapshot(
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "40",
            "unrealized_pnl": "0",
            "fees": "10",
            "funding": "0",
        },
        second,
    )

    assert first["equity"] == "950"
    assert first["peak_equity"] == "1000"
    assert second["equity"] == "890"
    assert second["drawdown"] == "110"
    assert second["drawdown_pct"] == "11"
    assert recovered["equity"] == "1030"
    assert recovered["peak_equity"] == "1030"
    assert recovered["drawdown"] == "0"
    assert recovered["max_drawdown"] == "110"
    assert recovered["max_drawdown_pct"] == "11"


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "currency": "USDT",
            "starting_equity": "1000",
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "fees": "-1",
            "funding": "0",
        },
        {
            "currency": "usd-t",
            "starting_equity": "1000",
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "fees": "0",
            "funding": "0",
        },
        {
            "currency": "USDT",
            "starting_equity": "1e30",
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "fees": "0",
            "funding": "0",
        },
    ],
)
def test_financial_state_snapshot_rejects_invalid_accounting_inputs(snapshot):
    with pytest.raises(ValueError):
        _financial_state_snapshot(snapshot, None)
