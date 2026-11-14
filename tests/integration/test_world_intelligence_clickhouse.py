"""Optional real-ClickHouse contract test; skipped outside integration CI."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import pytest

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore


@pytest.mark.asyncio
async def test_clickhouse_event_and_cursor_round_trip():
    host = os.getenv("CLICKHOUSE_HOST")
    if not host:
        pytest.skip("CLICKHOUSE_HOST is not configured")
    client = await clickhouse_connect.get_async_client(
        host=host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
    )
    source_id = f"wiats-test-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    source = SourceEvidence(
        source_id=source_id,
        source_kind="test",
        uri="https://example.test/wiats-test",
        published_at=now - timedelta(seconds=1),
        first_seen_at=now,
        reliability=1.0,
    )
    event = IntelligenceEvent(
        event_id=uuid.uuid4().hex,
        event_type="test",
        title="World intelligence persistence test",
        entities=("TEST",),
        published_at=source.published_at,
        first_seen_at=now,
        source=source,
        sentiment=0.0,
        severity=0.0,
        novelty=0.0,
        confidence=0.0,
        verification="unverified",
    )
    try:
        store = ClickHouseIntelligenceStore(client)
        await store.ensure_schema()
        await store.persist_page(source_id, [event], "cursor-round-trip")
        cursor = await store.load_cursor(source_id)
        rows = await client.query(
            f"SELECT count() FROM {store.events_table} "
            "WHERE source_id = {source_id:String}",
            parameters={"source_id": source_id},
        )
        assert cursor == "cursor-round-trip"
        assert rows.result_rows[0][0] == 1

        # A delayed prior owner must not replace a newer fenced checkpoint.
        await store.save_cursor(source_id, "cursor-newer", fencing_token=20)
        await store.save_cursor(source_id, "cursor-stale", fencing_token=19)
        assert await store.load_cursor(source_id) == "cursor-newer"

        failure_id = await store.record_dead_letter(
            source_id=source_id,
            cursor_before="cursor-newer",
            cursor_after="cursor-after-replay",
            events=[event],
            error=RuntimeError("redis unavailable"),
        )
        pending = await store.load_pending_dead_letters(limit=10)
        matching = [item for item in pending if item["failure_id"] == failure_id]
        assert len(matching) == 1
        assert matching[0]["events"][0].event_id == event.event_id
        await store.update_dead_letter(failure_id, status="replayed", attempts=2)
        pending = await store.load_pending_dead_letters(limit=10)
        assert failure_id not in {item["failure_id"] for item in pending}

        await store.record_source_run(
            source_id=source_id,
            started_at=now - timedelta(seconds=2),
            finished_at=now,
            status="success",
            event_count=1,
            cursor_before="cursor-newer",
            cursor_after="cursor-after",
            fencing_token=20,
            latency_ms=15.5,
        )
        source_summary = await store.load_source_run_summary(as_of=now)
        source_row = next(
            item for item in source_summary if item["source_id"] == source_id
        )
        assert source_row["runs"] == 1
        assert source_row["successes"] == 1
        assert source_row["events"] == 1
    finally:
        await client.close()
