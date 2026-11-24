import os
import uuid

import pytest

from aitos.execution.postgres_lifecycle_writer import PostgresLifecycleStateWriter
from aitos.execution.postgres_oms import OMSConflict, PostgresCanonicalOMS
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.storage.postgres_migrations import apply_postgres_migrations


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
