import os
import uuid

import pytest

from aitos.execution.postgres_intents import PostgresCanonicalExecutionIntentStore
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
        assert await apply_postgres_migrations(pool) == 5
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
        assert trade_events[0].payload["causality"]["causation_id"] == applied[
            "last_event_id"
        ]
        ledger = await oms.financial_trade_ledger("paper:intent-test", "trade-001")
        assert len(ledger) == 2
        assert sum(item["debit"] for item in ledger) == sum(
            item["credit"] for item in ledger
        ) == 650
        assert len(await oms.claim_outbox()) == 4

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
