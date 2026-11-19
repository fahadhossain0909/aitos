import json
from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.historical_ingestion import (
    HistoricalIngestionStore,
    HistoricalProviderPage,
    SourceProvenance,
)
from aitos.world_intelligence.point_in_time import EventRevision, MarketBarRevision

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows=()):
        self.result_rows = list(rows)


class FakeClient:
    def __init__(self):
        self.commands = []
        self.inserts = []
        self.queries = []

    async def command(self, query):
        self.commands.append(query)

    async def insert(self, table, data, column_names):
        self.inserts.append((table, data, column_names))

    async def query(self, query, parameters=None):
        self.queries.append((query, parameters))
        return Result()


class Provider:
    source_id = "official-feed"

    def __init__(self):
        self.seen_cursor = "initial"
        self.page = HistoricalProviderPage(
            source_id=self.source_id,
            next_cursor="next-page",
                raw_payload=json.dumps({"headline":"macro"}).encode(),
                metadata={"provider_revision":"v2"},
            ),),
        )

    async def fetch_page(self, *, cursor):
        self.seen_cursor = cursor
        return self.page


@pytest.mark.asyncio
async def test_historical_ingestion_persists_raw_provenance_revisions_then_cursor():
    client = FakeClient()
    store = HistoricalIngestionStore(client)
    provider = Provider()
    await store.ensure_schema()
    result = await store.ingest(provider, cursor="initial")
    assert result == "next-page"
    assert provider.seen_cursor == "initial"
    names = [table.rsplit(".", 1)[-1] for table, _, _ in client.inserts]
    assert names == [
        "world_intelligence_source_provenance",
        "world_intelligence_event_revisions",
        "world_intelligence_market_bar_revisions",
        "world_intelligence_historical_cursors",
    ]
    provenance_row = client.inserts[0][1][0]
    assert provenance_row[3] == "https://example.test/story"
    assert "secret" not in provenance_row[3]
    assert (
        provenance_row[7]
        == __import__("hashlib")
        .sha256(provider.page.provenance[0].raw_payload)
        .hexdigest()
    )


@pytest.mark.asyncio
async def test_historical_cursor_load_uses_durable_checkpoint():
    client = FakeClient()
    store = HistoricalIngestionStore(client)
    client.query = lambda query, parameters=None: _async_result([("cursor-42",)])

    async def load():
        result = await store.load_cursor("official-feed")
        assert result == "cursor-42"

    await load()


async def _async_result(rows):
    return Result(rows)


def test_source_provenance_rejects_naive_time_and_oversized_payload():
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceProvenance(
            "s",
            "kind",
            "r",
            "https://example.test",
            NOW,
            datetime(2026, 11, 19),
            b"x",
            {},
        )
    with pytest.raises(ValueError, match="512KB"):
        SourceProvenance(
            "s", "kind", "r", "https://example.test", NOW, NOW, b"x" * 512001, {}
        )
