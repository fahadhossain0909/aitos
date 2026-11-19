"""Optional real-ClickHouse point-in-time revision round-trip test."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import pytest

from aitos.world_intelligence.point_in_time import EventRevision, MarketBarRevision
from aitos.world_intelligence.point_in_time_storage import ClickHousePointInTimeStore


@pytest.mark.asyncio
async def test_clickhouse_point_in_time_revisions_round_trip():
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
    suffix = uuid.uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=0)
    asset = f"TEST{suffix[:8].upper()}"
    event = EventRevision(
        event_id=f"event-{suffix}",
        revision_id=f"revision-{suffix}",
        asset=asset,
        event_type="macro",
        published_at=now - timedelta(minutes=2),
        known_at=now - timedelta(minutes=1),
        sentiment=0.4,
        severity=0.8,
        novelty=0.7,
        confidence=0.9,
        source_reliability=0.95,
        regime="test",
        beta=0.2,
    )
    bar = MarketBarRevision(
        asset=asset,
        revision_id=f"bar-{suffix}",
        open_time=now,
        close_time=now + timedelta(minutes=1),
        available_at=now + timedelta(minutes=1),
        open_price=100.0,
        close_price=101.0,
    )
    try:
        store = ClickHousePointInTimeStore(client)
        await store.ensure_schema()
        assert await store.append_event_revisions([event]) == 1
        assert await store.append_market_bar_revisions([bar]) == 1
        events, bars = await store.load_revisions(as_of=now + timedelta(minutes=2))
        assert event in events
        assert bar in bars
    finally:
        await client.close()
