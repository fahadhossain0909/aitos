from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.point_in_time import EventRevision, MarketBarRevision
from aitos.world_intelligence.point_in_time_storage import ClickHousePointInTimeStore

NOW = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows=()):
        self.result_rows = list(rows)


class FakeClickHouse:
    def __init__(self):
        self.commands = []
        self.inserts = []
        self.queries = []
        self.query_rows = []

    async def command(self, query):
        self.commands.append(query)

    async def insert(self, table, data, column_names):
        self.inserts.append((table, data, column_names))

    async def query(self, query, parameters=None):
        self.queries.append((query, parameters))
        return Result(self.query_rows.pop(0) if self.query_rows else ())


def event_revision(revision_id="r1", known_at=None):
    return EventRevision(
        event_id="event-1",
        revision_id=revision_id,
        asset="btcusdt",
        event_type="macro",
        published_at=NOW - timedelta(minutes=2),
        known_at=known_at or NOW - timedelta(minutes=1),
        sentiment=0.4,
        severity=0.8,
        novelty=0.7,
        confidence=0.9,
        source_reliability=0.95,
        regime="trend",
        beta=0.3,
    )


def bar_revision(revision_id="bar-r1"):
    return MarketBarRevision(
        asset="BTCUSDT",
        revision_id=revision_id,
        open_time=NOW,
        close_time=NOW + timedelta(minutes=1),
        available_at=NOW + timedelta(minutes=1),
        open_price=90000,
        close_price=90100,
    )


@pytest.mark.asyncio
async def test_revision_schema_and_append_are_versioned_and_replay_safe():
    client = FakeClickHouse()
    store = ClickHousePointInTimeStore(client)
    await store.ensure_schema()
    assert any("world_intelligence_event_revisions" in q for q in client.commands)
    assert any("world_intelligence_market_bar_revisions" in q for q in client.commands)

    assert await store.append_event_revisions([event_revision()]) == 1
    assert await store.append_market_bar_revisions([bar_revision()]) == 1
    assert client.inserts[0][0].endswith("world_intelligence_event_revisions")
    assert client.inserts[0][1][0][1] == "r1"
    assert client.inserts[1][0].endswith("world_intelligence_market_bar_revisions")
    assert client.inserts[1][1][0][2] == "bar-r1"


@pytest.mark.asyncio
async def test_duplicate_revision_identity_is_rejected_before_insert():
    client = FakeClickHouse()
    store = ClickHousePointInTimeStore(client)
    with pytest.raises(ValueError, match="duplicate event revision"):
        await store.append_event_revisions([event_revision(), event_revision()])
    with pytest.raises(ValueError, match="duplicate market bar revision"):
        await store.append_market_bar_revisions([bar_revision(), bar_revision()])
    assert not client.inserts


@pytest.mark.asyncio
async def test_load_revisions_rehydrates_utc_dataclasses_and_point_in_time_filter():
    client = FakeClickHouse()
    store = ClickHousePointInTimeStore(client)
    event = event_revision()
    bar = bar_revision()
    import json

    event_payload = {
        "event_id": event.event_id,
        "revision_id": event.revision_id,
        "asset": event.asset,
        "event_type": event.event_type,
        "published_at": event.published_at.isoformat(),
        "known_at": event.known_at.isoformat(),
        "sentiment": event.sentiment,
        "severity": event.severity,
        "novelty": event.novelty,
        "confidence": event.confidence,
        "source_reliability": event.source_reliability,
        "regime": event.regime,
        "beta": event.beta,
    }
    bar_payload = {
        "asset": bar.asset,
        "revision_id": bar.revision_id,
        "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(),
        "available_at": bar.available_at.isoformat(),
        "open_price": bar.open_price,
        "close_price": bar.close_price,
    }
    client.query_rows = [[(json.dumps(event_payload),)], [(json.dumps(bar_payload),)]]
    events, bars = await store.load_revisions(as_of=NOW + timedelta(minutes=2))
    assert events[0] == event
    assert bars[0] == bar
    assert len(client.queries) == 2
    assert all("ingested_at <= " in query for query, _ in client.queries)


@pytest.mark.asyncio
async def test_load_revisions_rejects_naive_cutoff_and_invalid_limit():
    store = ClickHousePointInTimeStore(FakeClickHouse())
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.load_revisions(as_of=datetime(2026, 11, 19))
    with pytest.raises(ValueError, match="limit"):
        await store.load_revisions(as_of=NOW, limit=0)


def test_database_identifier_is_validated():
    with pytest.raises(ValueError, match="identifier"):
        ClickHousePointInTimeStore(FakeClickHouse(), "aitos; DROP TABLE x")
