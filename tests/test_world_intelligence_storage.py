from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(source_id="gdelt"):
    source = SourceEvidence(
        source_id=source_id,
        source_kind="news",
        uri="https://example.test/story/1",
        published_at=NOW - timedelta(minutes=1),
        first_seen_at=NOW,
        reliability=0.7,
    )
    return IntelligenceEvent(
        event_id="event-1",
        event_type="news",
        title="Example headline",
        entities=("BTC",),
        published_at=NOW - timedelta(minutes=1),
        first_seen_at=NOW,
        source=source,
        sentiment=0.0,
        severity=0.2,
        novelty=0.5,
        confidence=0.2,
    )


class FakeResult:
    def __init__(self, rows=()):
        self.result_rows = list(rows)


class FakeAsyncClickHouse:
    def __init__(self):
        self.commands = []
        self.inserts = []
        self.fail_events = False
        self.fail_cursor = False
        self.cursor = None

    async def command(self, query):
        self.commands.append(query)

    async def insert(self, table, data, column_names):
        if table.endswith("world_intelligence_events"):
            if self.fail_events:
                raise RuntimeError("event insert failed")
        elif table.endswith("world_intelligence_cursors"):
            if self.fail_cursor:
                raise RuntimeError("cursor insert failed")
            self.cursor = data[0][1]
        self.inserts.append((table, data, column_names))

    async def query(self, query, parameters=None):
        return FakeResult([(self.cursor,)] if self.cursor is not None else [])


@pytest.mark.asyncio
async def test_schema_creates_event_and_cursor_tables():
    client = FakeAsyncClickHouse()
    await ClickHouseIntelligenceStore(client).ensure_schema()
    assert len(client.commands) == 2
    assert "ReplacingMergeTree" in client.commands[0]
    assert "world_intelligence_cursors" in client.commands[1]


@pytest.mark.asyncio
async def test_page_persists_events_before_cursor_and_loads_checkpoint():
    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    count = await store.persist_page("gdelt", [make_event()], "page-2")
    assert count == 1
    assert client.inserts[0][0].endswith("world_intelligence_events")
    assert client.inserts[1][0].endswith("world_intelligence_cursors")
    assert await store.load_cursor("gdelt") == "page-2"


@pytest.mark.asyncio
async def test_event_failure_never_advances_cursor():
    client = FakeAsyncClickHouse()
    client.fail_events = True
    store = ClickHouseIntelligenceStore(client)
    with pytest.raises(RuntimeError, match="event insert failed"):
        await store.persist_page("gdelt", [make_event()], "page-2")
    assert client.cursor is None


@pytest.mark.asyncio
async def test_cursor_failure_is_propagated_after_event_persistence():
    client = FakeAsyncClickHouse()
    client.fail_cursor = True
    store = ClickHouseIntelligenceStore(client)
    with pytest.raises(RuntimeError, match="cursor insert failed"):
        await store.persist_page("gdelt", [make_event()], "page-2")
    assert len(client.inserts) == 1
    assert client.inserts[0][0].endswith("world_intelligence_events")


@pytest.mark.asyncio
async def test_empty_page_can_advance_cursor():
    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    assert await store.persist_page("gdelt", [], "page-3") == 0
    assert len(client.inserts) == 1
    assert await store.load_cursor("gdelt") == "page-3"


@pytest.mark.asyncio
async def test_page_rejects_cross_source_event():
    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    with pytest.raises(ValueError, match="does not match"):
        await store.persist_page("sec", [make_event("gdelt")], "cursor")
    assert not client.inserts


def test_database_name_is_validated_before_sql_interpolation():
    with pytest.raises(ValueError):
        ClickHouseIntelligenceStore(FakeAsyncClickHouse(), "aitos; DROP TABLE x")


@pytest.mark.asyncio
async def test_publish_failure_does_not_advance_durable_cursor():
    from aitos.world_intelligence.adapters import ingest_page_durably

    class Adapter:
        source_id = "gdelt"

        async def poll(self, *, cursor=None):
            return [make_event()], "page-2"

    class FailingSink:
        async def publish(self, topic, payload):
            raise RuntimeError("redis unavailable")

    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    with pytest.raises(RuntimeError, match="redis unavailable"):
        await ingest_page_durably(Adapter(), FailingSink(), store)
    assert len(client.inserts) == 1
    assert client.inserts[0][0].endswith("world_intelligence_events")
    assert client.cursor is None


@pytest.mark.asyncio
async def test_durable_ingestion_checkpoints_after_publish():
    from aitos.world_intelligence.adapters import ingest_page_durably

    class Adapter:
        source_id = "gdelt"

        async def poll(self, *, cursor=None):
            return [make_event()], "page-2"

    class RecordingSink:
        def __init__(self):
            self.published = []

        async def publish(self, topic, payload):
            self.published.append((topic, payload))

    client = FakeAsyncClickHouse()
    sink = RecordingSink()
    store = ClickHouseIntelligenceStore(client)
    result = await ingest_page_durably(Adapter(), sink, store)
    assert result == "page-2"
    assert len(sink.published) == 1
    assert client.inserts[-1][0].endswith("world_intelligence_cursors")
    assert await store.load_cursor("gdelt") == "page-2"


@pytest.mark.asyncio
async def test_none_cursor_uses_empty_string_checkpoint_sentinel():
    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    await store.save_cursor("gdelt", None)
    assert client.inserts[-1][1][0][1] == ""
    assert await store.load_cursor("gdelt") is None


@pytest.mark.asyncio
async def test_load_recent_events_rehydrates_validated_point_in_time_payload():
    import json

    client = FakeAsyncClickHouse()
    store = ClickHouseIntelligenceStore(client)
    event = make_event()
    client.query = async_query = lambda query, parameters=None: FakeResult(
        [(event.to_json(),)]
    )
    loaded = await store.load_recent_events(as_of=NOW, lookback_hours=24)
    assert loaded[0].event_id == event.event_id
    assert loaded[0].source.source_id == event.source.source_id


@pytest.mark.asyncio
async def test_load_recent_events_rejects_naive_as_of():
    store = ClickHouseIntelligenceStore(FakeAsyncClickHouse())
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.load_recent_events(as_of=datetime(2026, 11, 13, 12, 0))
