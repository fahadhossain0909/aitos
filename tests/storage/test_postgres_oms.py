import json
import os
import sys
import uuid
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from aitos.execution.postgres_lifecycle_writer import PostgresLifecycleStateWriter
from aitos.execution.postgres_oms import OMSConflict, PostgresCanonicalOMS
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.storage.postgres_migrations import apply_postgres_migrations
from scripts.import_clickhouse_state_to_postgres_oms import run as run_clickhouse_oms_import


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
        )
        duplicate = await oms.append(
            fence,
            aggregate_id="trade-1",
            event_id="event-1",
            expected_revision=0,
            payload={"state": "OPEN", "quantity": "1"},
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

        projected = []

        async def projection_writer(trade):
            projected.append(trade.to_dict())

        writer = PostgresLifecycleStateWriter(oms, second_fence, projection_writer)

        class TradeSnapshot:
            trade_id = "trade-1"

            @staticmethod
            def to_dict():
                return {"trade_id": "trade-1", "state": "POSITION_OPENED"}

        await writer(TradeSnapshot())
        assert projected == [{"trade_id": "trade-1", "state": "POSITION_OPENED"}]
        assert (await writer.latest_snapshot("trade-1"))["state"] == "POSITION_OPENED"

        async def broken_projection(_trade):
            raise RuntimeError("projection unavailable")

        broken = PostgresLifecycleStateWriter(oms, second_fence, broken_projection)
        with pytest.raises(RuntimeError, match="projection unavailable"):
            await broken(TradeSnapshot())
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
        assert [
            item.revision
            for item in await oms.read_events("paper:integration", "trade-1")
        ] == [1, 2, 3, 4]
        pending = await oms.claim_outbox()
        assert len(pending) == 5
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
                    [[
                        (
                            "import-open",
                            "BTCUSDT",
                            "position_opened",
                            runtime_payload,
                            "2026-11-24T10:00:00Z",
                        )
                    ]]
                )
            else:
                yield iter(
                    [[
                        (
                            "import-closed",
                            closed_payload,
                            "2026-11-24T11:00:00Z",
                        )
                    ]]
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
            await admin.execute(
                "UPDATE aitos_oms_legacy_imports SET raw_payload='{}'"
            )
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute("DELETE FROM aitos_oms_legacy_imports")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await admin.execute(
                "UPDATE aitos_oms_events SET event_id='tampered'"
            )
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

        recovery = await recovery_writer.restore_lifecycle(RecoveredLifecycle())
        assert recovery == {"open": 1, "closed": 1}
        assert RecoveredLifecycle._open_trades["import-open"].quantity == 0.125
        assert RecoveredLifecycle._closed_trades[0].trade_id == "import-closed"
    finally:
        if not pool.is_closing():
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
