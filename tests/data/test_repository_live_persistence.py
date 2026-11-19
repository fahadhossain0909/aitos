from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.data.repository import (
    CREATE_LIVE_ANALYTICS_EVENTS,
    CREATE_MARKET_EVENTS,
    CREATE_VERSIONED_MARKET_EVENTS,
    MarketDataRepository,
)
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource


def make_event() -> MarketEvent:
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=now,
        ingest_time=now,
        payload={"bid": 90000.0, "ask": 90001.0},
        source=MarketSource.WEBSOCKET,
    )


class FakeClient:
    def __init__(self) -> None:
        self.inserts = []
        self.queries = []

    async def query(self, sql, parameters, settings):
        self.queries.append((sql, parameters, settings))
        return SimpleNamespace(
            column_names=["event_time", "event_id", "payload_json"],
            result_rows=[
                (parameters["start_time"], "evt-spot-1", '{"last_price":"101"}')
            ],
        )

    async def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))


@pytest.mark.asyncio
async def test_save_market_event_persists_canonical_envelope():
    repo = MarketDataRepository.__new__(MarketDataRepository)
    repo._client = FakeClient()
    repo._initialized = True
    repo._last_event_time = None

    event = make_event()
    await repo.save_market_event(event)

    table, rows, columns = repo._client.inserts[0]
    assert table == "market_events"
    assert rows[0][2] == event.event_id
    assert rows[0][5] == "binance"
    assert rows[0][6] == "usd_m_futures"
    assert rows[0][7] == "BTCUSDT"
    assert rows[0][8] == "BTCUSDT"
    assert rows[0][9] == "ticker"
    assert "payload_json" in columns
    assert repo._last_event_time == event.event_time.isoformat()


def test_live_persistence_schema_contains_required_contracts():
    assert "event_time DateTime64(3, 'UTC')" in CREATE_MARKET_EVENTS
    assert "ingest_time DateTime64(3, 'UTC')" in CREATE_MARKET_EVENTS
    assert "payload_json String" in CREATE_MARKET_EVENTS
    assert "market_type LowCardinality(String)" in CREATE_MARKET_EVENTS
    assert "instrument_id String" in CREATE_MARKET_EVENTS
    assert (
        "ORDER BY (instrument_id, event_type, event_time, event_id)"
        in CREATE_MARKET_EVENTS
    )
    assert "category LowCardinality(String)" in CREATE_LIVE_ANALYTICS_EVENTS
    assert "source_module LowCardinality(String)" in CREATE_LIVE_ANALYTICS_EVENTS
    assert "payload_json String" in CREATE_LIVE_ANALYTICS_EVENTS



@pytest.mark.asyncio
async def test_save_spot_market_event_persists_isolated_instrument_identity():
    repo = MarketDataRepository.__new__(MarketDataRepository)
    repo._client = FakeClient()
    repo._initialized = True
    repo._last_event_time = None

    now = datetime.now(timezone.utc)
    event = MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="spot",
        market_type="spot",
        venue="binance",
        instrument_id="binance:spot:BTCUSDT",
        symbol="BTCUSDT",
        event_time=now,
        ingest_time=now,
        payload={"last_price": "100"},
        source=MarketSource.WEBSOCKET,
    )
    await repo.save_market_event(event)

    table, rows, columns = repo._client.inserts[0]
    assert table == "market_events"
    assert rows[0][6] == "spot"
    assert rows[0][7] == "binance:spot:BTCUSDT"
    assert rows[0][8] == "BTCUSDT"
    assert columns.index("market_type") == 6
    assert columns.index("instrument_id") == 7



@pytest.mark.asyncio
async def test_save_versioned_spot_market_event_preserves_revision_and_identity():
    repo = MarketDataRepository.__new__(MarketDataRepository)
    repo._client = FakeClient()
    repo._initialized = True
    repo._last_event_time = None
    now = datetime.now(timezone.utc)
    event = MarketEvent(
        event_type=MarketEventType.KLINE,
        exchange="binance",
        market="spot",
        market_type="spot",
        instrument_id="binance:spot:BTCUSDT",
        symbol="BTCUSDT",
        event_time=now,
        ingest_time=now,
        payload={"close": "101.5", "is_closed": True},
        source=MarketSource.REST,
        event_revision=7,
    )

    await repo.save_market_event_versioned(event)

    table, rows, columns = repo._client.inserts[0]
    assert table == "market_event_versions"
    assert rows[0][2] == event.event_id
    assert rows[0][3] == 7
    assert rows[0][7] == "spot"
    assert rows[0][8] == "binance:spot:BTCUSDT"
    assert "event_revision" in columns
    assert "payload_json" in columns


def test_versioned_event_schema_uses_replacing_revision_semantics():
    assert "event_revision UInt64" in CREATE_VERSIONED_MARKET_EVENTS
    assert "ReplacingMergeTree(event_revision)" in CREATE_VERSIONED_MARKET_EVENTS
    assert "ORDER BY (instrument_id, event_id)" in CREATE_VERSIONED_MARKET_EVENTS



@pytest.mark.asyncio
async def test_load_versioned_events_filters_exact_spot_instrument_and_decodes_payload():
    repo = MarketDataRepository.__new__(MarketDataRepository)
    repo._client = FakeClient()
    repo._initialized = True
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)

    rows = await repo.load_versioned_market_events(
        market_type="spot",
        instrument_id="binance:spot:BTCUSDT",
        event_types=("ticker",),
        start_time=start,
        end_time=end,
        limit=1000,
    )

    sql, parameters, _settings = repo._client.queries[0]
    assert "market_event_versions FINAL" in sql
    assert parameters["market_type"] == "spot"
    assert parameters["instrument_id"] == "binance:spot:BTCUSDT"
    assert parameters["event_types"] == ["ticker"]
    assert rows[0]["payload"] == {"last_price": "101"}


@pytest.mark.asyncio
async def test_versioned_event_loader_rejects_naive_or_reversed_time_ranges():
    repo = MarketDataRepository.__new__(MarketDataRepository)
    repo._initialized = True
    start = datetime(2026, 11, 1)
    end = start + timedelta(hours=1)

    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.load_versioned_market_events(
            market_type="spot",
            instrument_id="binance:spot:BTCUSDT",
            start_time=start,
            end_time=end,
        )
