import os
import uuid

import pytest

from aitos.execution.postgres_oms import OMSConflict, PostgresCanonicalOMS
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
        fence = await oms.acquire_writer(
            "paper:integration", "worker-a", expected_generation=0
        )
        first = await oms.append(
            fence,
            aggregate_id="trade-1",
            event_id="event-1",
            expected_revision=0,
            payload={"state": "OPEN", "quantity": "1"},
        )
        duplicate = await oms.append(
            fence, aggregate_id="trade-1", event_id="event-1",
            expected_revision=0, payload={"state": "OPEN", "quantity": "1"},
        )
        assert duplicate.event_hash == first.event_hash
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
        assert [
            item.revision
            for item in await oms.read_events("paper:integration", "trade-1")
        ] == [1, 2]
        pending = await oms.claim_outbox()
        assert len(pending) == 2
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
