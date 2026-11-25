import json
import os
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest

from aitos.execution.postgres_lifecycle_writer import PostgresLifecycleStateWriter
from aitos.execution.postgres_oms import OMSConflict, PostgresCanonicalOMS
from aitos.execution.postgres_outbox_projector import PostgresOMSOutboxProjector
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.storage.postgres_migrations import apply_postgres_migrations
from scripts.import_clickhouse_state_to_postgres_oms import (
    run as run_clickhouse_oms_import,
)


def test_postgres_oms_financial_journal_requires_balanced_finite_lines():
    from aitos.execution.postgres_oms import PostgresCanonicalOMS

    balanced = PostgresCanonicalOMS._validate_ledger_entries(
        [
            {
                "account_code": "EXPENSE_FEES",
                "currency": "USDT",
                "debit": "0.1",
                "credit": "0",
            },
            {
                "account_code": "CASH",
                "currency": "USDT",
                "debit": "0",
                "credit": "0.1",
            },
        ]
    )
    assert len(balanced) == 2
    with pytest.raises(ValueError, match="balance"):
        PostgresCanonicalOMS._validate_ledger_entries(
            [
                {
                    "account_code": "EXPENSE_FEES",
                    "currency": "USDT",
                    "debit": "0.1",
                    "credit": "0",
                },
                {
                    "account_code": "CASH",
                    "currency": "USDT",
                    "debit": "0",
                    "credit": "0.09",
                },
            ]
        )
    with pytest.raises(ValueError, match="exactly one"):
        PostgresCanonicalOMS._validate_ledger_entries(
            [
                {
                    "account_code": "CASH",
                    "currency": "USDT",
                    "debit": "1",
                    "credit": "1",
                }
            ]
        )


@pytest.mark.asyncio
async def test_postgres_oms_fencing_cas_idempotency_and_hash_chain():
    dsn = os.getenv("AITOS_POSTGRES_OMS_TEST_DSN")
    if not dsn:
        pytest.skip("AITOS_POSTGRES_OMS_TEST_DSN is not configured")
    import asyncpg

    schema = "aitos_oms_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    try:
        await apply_postgres_migrations(pool)
        oms = PostgresCanonicalOMS(pool)
        assert await oms.writer_generation("paper:integration") == 0
        fence = await oms.acquire_writer(
            "paper:integration",
            "worker-a",
            expected_generation=0,
        )
        assert await oms.writer_generation("paper:integration") == 1
        first = await oms.append(
            fence,
            aggregate_id="trade-1",
            event_id="event-1",
            expected_revision=0,
            payload={"state": "OPEN", "quantity": "1"},
            ledger_entries=[
                {
                    "account_code": "POSITION_ASSET",
                    "currency": "USDT",
                    "debit": "100.25",
                    "credit": "0",
                    "reference": "entry-notional",
                },
                {
                    "account_code": "CASH_CLEARING",
                    "currency": "USDT",
                    "debit": "0",
                    "credit": "100.25",
                    "reference": "entry-notional",
                },
            ],
            causation_id="signal:abc",
            correlation_id="trade-1",
            parent_event_id="signal-event-1",
        )
        assert first.payload["causality"] == {
            "causation_id": "signal:abc",
            "correlation_id": "trade-1",
            "parent_event_id": "signal-event-1",
        }
        balances = await oms.financial_balances("paper:integration")
        assert len(balances) == 2
        assert sum((row["debit"] for row in balances), start=Decimal("0")) == Decimal(
            "100.25"
        )
        assert sum((row["credit"] for row in balances), start=Decimal("0")) == Decimal(
            "100.25"
        )
        trade_ledger = await oms.financial_trade_ledger("paper:integration", "trade-1")
        assert len(trade_ledger) == 2
        duplicate = await oms.append(
            fence,
            aggregate_id="trade-1",
            event_id="event-1",
            expected_revision=0,
            payload={"state": "OPEN", "quantity": "1"},
            ledger_entries=[
                {
                    "account_code": "POSITION_ASSET",
                    "currency": "USDT",
                    "debit": "100.25",
                    "credit": "0",
                    "reference": "entry-notional",
                },
                {
                    "account_code": "CASH_CLEARING",
                    "currency": "USDT",
                    "debit": "0",
                    "credit": "100.25",
                    "reference": "entry-notional",
                },
            ],
            causation_id="signal:abc",
            correlation_id="trade-1",
            parent_event_id="signal-event-1",
        )
        assert duplicate.event_hash == first.event_hash
        assert await oms.list_aggregate_ids("paper:integration") == ["trade-1"]
        second_fence = await oms.acquire_writer(
            "paper:integration",
            "worker-b",
            expected_generation=1,
        )
        with pytest.raises(OMSConflict, match="stale writer"):
            await oms.append(
                fence,
                aggregate_id="trade-1",
                event_id="event-2",
                expected_revision=1,
                payload={"state": "CLOSED"},
            )
        with pytest.raises(OMSConflict, match="revision CAS"):
            await oms.append(
                second_fence,
                aggregate_id="trade-1",
                event_id="event-3",
                expected_revision=0,
                payload={"state": "CLOSED"},
            )
        closed = await oms.append(
            second_fence,
            aggregate_id="trade-1",
            event_id="event-4",
            expected_revision=1,
            payload={"state": "CLOSED"},
        )
        assert closed.revision == 2
        with pytest.raises(asyncpg.PostgresError, match="not balanced"):
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO aitos_oms_financial_ledger
                           (account_scope,event_id,line_no,trade_id,account_code,
                            currency,debit,credit)
                           VALUES($1,$2,0,$3,'INVALID_POSTING','USDT',1,0)""",
                        "paper:integration",
                        "event-4",
                        "trade-1",
                    )

        projected = []

        async def projection_writer(trade):
            projected.append(trade.to_dict())

        writer = PostgresLifecycleStateWriter(oms, second_fence, projection_writer)

        trade_snapshot = Trade(
            trade_id="trade-1",
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_price=100.0,
            quantity=0.1,
            leverage=2.0,
            position_size_usd=10.0,
            risk_amount_usd=1.0,
            strategy_id="integration",
            agent_consensus={},
            explanation="canonical OMS projection fixture",
            sl_price=95.0,
            tp_price=110.0,
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time="2026-11-24T00:00:00+00:00",
            account_scope="paper:integration",
        )

        await writer(trade_snapshot)
        assert projected == [trade_snapshot.to_dict()]
        assert (await writer.latest_snapshot("trade-1"))["state"] == "POSITION_OPENED"

        async def broken_projection(_trade):
            raise RuntimeError("projection unavailable")

        broken = PostgresLifecycleStateWriter(oms, second_fence, broken_projection)
        with pytest.raises(RuntimeError, match="projection unavailable"):
            await broken(trade_snapshot)
        # Canonical event is committed before the projection callback fails.
        assert (await broken.latest_snapshot("trade-1"))["state"] == "POSITION_OPENED"
        restored_trade = Trade(
            trade_id="trade-restore",
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_price=100.0,
            quantity=0.1,
            leverage=2.0,
            position_size_usd=10.0,
            risk_amount_usd=1.0,
            strategy_id="integration",
            agent_consensus={},
            explanation="OMS recovery integration",
            sl_price=95.0,
            tp_price=110.0,
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time="2026-11-24T00:00:00+00:00",
            account_scope="paper:recovery",
        )
        recovery_fence = await oms.acquire_writer(
            "paper:recovery",
            "worker-c",
            expected_generation=0,
        )
        recovery_writer = PostgresLifecycleStateWriter(
            oms, recovery_fence, projection_writer
        )
        await recovery_writer(restored_trade)

        async def fail_recovery_projection(_trade):
            raise RuntimeError("projection interrupted after canonical commit")

        interrupted_writer = PostgresLifecycleStateWriter(
            oms, recovery_fence, fail_recovery_projection
        )
        with pytest.raises(RuntimeError, match="projection interrupted"):
            await interrupted_writer(restored_trade)
        projector = PostgresOMSOutboxProjector(
            oms,
            recovery_writer,
            worker_id="integration-outbox-recovery",
            batch_size=10,
            lease_seconds=30,
        )
        replay = await projector.run_once()
        assert replay == {"claimed": 4, "projected": 4, "failed": 0}
        assert projected[-1]["trade_id"] == "trade-restore"

        # Binance fee/funding records become immutable, balanced postings in
        # the same canonical trade event chain after lifecycle write-through.
        await recovery_writer.latest_snapshot("trade-restore")
        fee_event = await recovery_writer.append_cost_adjustment(
            "trade-restore", "binance:income:fee-9001", "fee", "0.25"
        )
        funding_event = await recovery_writer.append_cost_adjustment(
            "trade-restore", "binance:income:funding-9002", "funding", "-0.10"
        )
        duplicate_fee = await recovery_writer.append_cost_adjustment(
            "trade-restore", "binance:income:fee-9001", "fee", "0.25"
        )
        assert duplicate_fee.event_hash == fee_event.event_hash
        assert funding_event.revision == fee_event.revision + 1
        assert fee_event.payload["event_type"] == "TRADE_COST_ADJUSTMENT"
        ledger_lines = await oms.financial_trade_ledger(
            "paper:recovery", "trade-restore"
        )
        assert len(ledger_lines) == 4
        assert sum((line["debit"] for line in ledger_lines), Decimal("0")) == Decimal(
            "0.35"
        )
        assert sum((line["credit"] for line in ledger_lines), Decimal("0")) == Decimal(
            "0.35"
        )
        assert {line["account_code"] for line in ledger_lines} == {
            "EXPENSE_FEES",
            "CASH",
            "FUNDING_EXPENSE",
        }

        class LifecycleState:
            _open_trades = {"trade-restore": restored_trade}
            _closed_trades = []

        recovery = await recovery_writer.restore_lifecycle(LifecycleState())
        assert recovery == {"open": 1, "closed": 0}
        assert list(LifecycleState._open_trades) == ["trade-restore"]
        assert LifecycleState._open_trades["trade-restore"].symbol == "BTCUSDT"
        assert (
            LifecycleState._open_trades["trade-restore"].account_scope
            == "paper:recovery"
        )
        # Canonical intent and financial aggregates share the journal but
        # must never be hydrated as TradeLifecycle positions.
        await oms.append(
            recovery_fence,
            aggregate_id="intent-client-1",
            event_id="intent:client-1:revision:1",
            expected_revision=0,
            payload={"event_type": "EXECUTION_INTENT_CREATED", "state": "SUBMITTING"},
        )
        await oms.append(
            recovery_fence,
            aggregate_id="financial:USDT",
            event_id="financial:USDT:revision:1",
            expected_revision=0,
            payload={
                "event_type": "FINANCIAL_STATE_SNAPSHOT",
                "financial_state": {"currency": "USDT"},
            },
        )
        recovered_with_non_trade_aggregates = await recovery_writer.restore_lifecycle(
            LifecycleState()
        )
        assert recovered_with_non_trade_aggregates == {"open": 1, "closed": 0}
        assert list(LifecycleState._open_trades) == ["trade-restore"]
        assert [
            item.revision
            for item in await oms.read_events("paper:integration", "trade-1")
        ] == [1, 2, 3, 4]
        pending = await oms.claim_outbox()
        assert len(pending) == 3

        first_claim = await oms.claim_outbox_leased(
            "projector-a", limit=1, lease_seconds=60
        )
        assert len(first_claim) == 1
        competing_claim = await oms.claim_outbox_leased(
            "projector-b", limit=1, lease_seconds=60
        )
        assert len(competing_claim) == 1
        assert (
            first_claim[0]["account_scope"],
            first_claim[0]["aggregate_id"],
            first_claim[0]["revision"],
        ) != (
            competing_claim[0]["account_scope"],
            competing_claim[0]["aggregate_id"],
            competing_claim[0]["revision"],
        )
        with pytest.raises(OMSConflict, match="not owned"):
            await oms.release_outbox_claim(
                first_claim[0],
                owner_id="projector-b",
                claim_token=str(first_claim[0]["claim_token"]),
                error="wrong worker",
            )
        await oms.release_outbox_claim(
            first_claim[0],
            owner_id="projector-a",
            claim_token=str(first_claim[0]["claim_token"]),
            error="simulated projection crash",
        )
        reclaimed = await oms.claim_outbox_leased(
            "projector-c", limit=1, lease_seconds=60
        )
        assert len(reclaimed) == 1
        assert reclaimed[0]["attempts"] == first_claim[0]["attempts"] + 1
        assert reclaimed[0]["event_hash"] == first_claim[0]["event_hash"]
        await oms.acknowledge_outbox_claim(
            reclaimed[0],
            owner_id="projector-c",
            claim_token=str(reclaimed[0]["claim_token"]),
        )
        with pytest.raises(OMSConflict, match="stale, expired, or already"):
            await oms.acknowledge_outbox_claim(
                reclaimed[0],
                owner_id="projector-c",
                claim_token=str(reclaimed[0]["claim_token"]),
            )

        await oms.mark_projected(
            "paper:integration",
            "trade-1",
            1,
            event_hash=first.event_hash,
        )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_outbox_full_rebuild_repairs_missing_projection_drift():
    dsn = os.getenv("AITOS_POSTGRES_OMS_TEST_DSN")
    if not dsn:
        pytest.skip("AITOS_POSTGRES_OMS_TEST_DSN is not configured")
    import asyncpg

    schema = "aitos_projection_rebuild_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    projected = []
    trade = Trade(
        trade_id="trade-projection-rebuild",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=65000.0,
        quantity=0.01,
        leverage=2.0,
        position_size_usd=650.0,
        risk_amount_usd=10.0,
        strategy_id="projection-rebuild",
        agent_consensus={},
        explanation="projection rebuild acceptance",
        sl_price=64000.0,
        tp_price=67000.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-25T00:00:00+00:00",
        account_scope="paper:projection-rebuild",
    )

    async def projection_writer(value):
        projected[:] = [value]

    async def projection_reader():
        return list(projected)

    try:
        assert await apply_postgres_migrations(pool) == 7
        oms = PostgresCanonicalOMS(pool)
        fence = await oms.acquire_writer(
            "paper:projection-rebuild",
            "projection-writer",
            expected_generation=0,
        )
        writer = PostgresLifecycleStateWriter(oms, fence, projection_writer)
        await writer(trade)
        projector = PostgresOMSOutboxProjector(
            oms,
            writer,
            worker_id="projection-rebuild-worker",
            projection_reader=projection_reader,
        )

        projected[0].quantity = 0.02
        drift = await projector.audit_scope()
        assert drift["in_sync"] is False
        assert drift["mismatched_trade_ids"] == ["trade-projection-rebuild"]

        repaired = await projector.audit_and_repair_scope()
        assert repaired["repaired"] is True
        assert repaired["rebuild"]["requeued"] == 1
        assert repaired["after"]["in_sync"] is True
        assert repaired["after"]["missing_trade_ids"] == []
        assert repaired["after"]["mismatched_trade_ids"] == []
        assert projected[0].quantity == 0.01

        with pytest.raises(ValueError, match="confirmation"):
            await projector.rebuild_scope(confirm=False)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_clickhouse_import_streams_lossless_payloads_into_postgres_oms(
    monkeypatch, tmp_path
):
    dsn = os.getenv("AITOS_POSTGRES_OMS_TEST_DSN")
    if not dsn:
        pytest.skip("AITOS_POSTGRES_OMS_TEST_DSN is not configured")
    import asyncpg

    schema = "aitos_import_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=5,
        server_settings={"search_path": schema},
    )

    runtime_trade = Trade(
        trade_id="import-open",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=0.125,
        leverage=2.0,
        position_size_usd=12.5,
        risk_amount_usd=1.0,
        strategy_id="legacy-import",
        agent_consensus={},
        explanation="legacy open import",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-24T10:00:00+00:00",
        account_scope="paper:import",
    )
    closed_trade = Trade(
        trade_id="import-closed",
        symbol="ETHUSDT",
        side=TradeSide.SHORT,
        entry_price=200.0,
        quantity=0.25,
        leverage=2.0,
        position_size_usd=50.0,
        risk_amount_usd=2.0,
        strategy_id="legacy-import",
        agent_consensus={},
        explanation="legacy closed import",
        sl_price=205.0,
        tp_price=190.0,
        state=TradeLifecycleState.POSITION_CLOSED,
        entry_time="2026-11-24T10:30:00+00:00",
        account_scope="paper:import",
    )
    runtime_payload = json.dumps(runtime_trade.to_dict(), separators=(",", ":"))
    closed_payload = json.dumps(closed_trade.to_dict(), separators=(",", ":"))

    class FakeClickHouse:
        closed = False

        def query(self, query):
            if "system.columns" in query:
                return SimpleNamespace(
                    result_rows=[
                        ("trade_runtime_state", "trade_id"),
                        ("paper_closed_trade_ledger", "trade_id"),
                    ]
                )
            raise AssertionError(f"unexpected ClickHouse query: {query}")

        @contextmanager
        def query_row_block_stream(self, query):
            if "FROM trade_runtime_state" in query:
                yield iter(
                    [
                        [
                            (
                                "import-open",
                                "BTCUSDT",
                                "position_opened",
                                runtime_payload,
                                "2026-11-24T10:00:00Z",
                            )
                        ]
                    ]
                )
            else:
                yield iter(
                    [
                        [
                            (
                                "import-closed",
                                closed_payload,
                                "2026-11-24T11:00:00Z",
                            )
                        ]
                    ]
                )

        def close(self):
            self.closed = True

    clickhouse_client = FakeClickHouse()
    clickhouse_module = ModuleType("clickhouse_connect")
    clickhouse_module.get_client = lambda **kwargs: clickhouse_client
    asyncpg_module = ModuleType("asyncpg")

    async def use_existing_pool(*args, **kwargs):
        return pool

    asyncpg_module.create_pool = use_existing_pool
    monkeypatch.setitem(sys.modules, "clickhouse_connect", clickhouse_module)
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_module)
    monkeypatch.setenv("DATABASE_URL", dsn)

    try:
        result = await run_clickhouse_oms_import(
            SimpleNamespace(
                apply=True,
                confirm_writers_stopped=True,
                confirm_backup_retained=True,
                scope_map={},
                manifest_out=tmp_path / "import-manifest.jsonl",
            )
        )
        assert result["mode"] == "imported-and-verified"
        assert result["imported_rows"] == 2
        assert result["canonical_events_verified"] == 2
        assert clickhouse_client.closed is True

        await admin.execute(f'SET search_path TO "{schema}"')
        evidence = await admin.fetch(
            "SELECT trade_id,raw_payload,raw_payload_sha256 "
            "FROM aitos_oms_legacy_imports ORDER BY trade_id"
        )
        assert [(row["trade_id"], row["raw_payload"]) for row in evidence] == [
            ("import-closed", closed_payload),
            ("import-open", runtime_payload),
        ]
        events = await admin.fetch(
            "SELECT aggregate_id,event_id,payload::text AS payload "
            "FROM aitos_oms_events ORDER BY aggregate_id"
        )
        assert len(events) == 2
        assert all(row["event_id"].startswith("legacy:") for row in events)
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute("UPDATE aitos_oms_legacy_imports SET raw_payload='{}'")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute("DELETE FROM aitos_oms_legacy_imports")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute("UPDATE aitos_oms_events SET event_id='tampered'")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute("DELETE FROM aitos_oms_events")

        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=3,
            server_settings={"search_path": schema},
        )
        oms = PostgresCanonicalOMS(pool)
        recovery_fence = await oms.acquire_writer(
            "paper:import",
            "recovery-reader",
            expected_generation=1,
        )

        async def projection_writer(_trade):
            return None

        recovery_writer = PostgresLifecycleStateWriter(
            oms, recovery_fence, projection_writer
        )
        restored = await recovery_writer.latest_snapshot("import-open")
        assert restored["trade_id"] == "import-open"
        assert restored["quantity"] == 0.125
        assert restored["account_scope"] == "paper:import"

        class RecoveredLifecycle:
            _open_trades = {}
            _closed_trades = []

        recovered_lifecycle = RecoveredLifecycle()
        recovery = await recovery_writer.restore_lifecycle(recovered_lifecycle)
        assert recovery == {"open": 1, "closed": 1}
        assert recovered_lifecycle._open_trades["import-open"].quantity == 0.125
        assert recovered_lifecycle._closed_trades[0].trade_id == "import-closed"
    finally:
        if not pool.is_closing():
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
