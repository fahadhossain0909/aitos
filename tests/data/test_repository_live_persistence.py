from datetime import datetime, timezone

import pytest

from aitos.data.repository import (
    CREATE_LIVE_ANALYTICS_EVENTS,
    CREATE_MARKET_EVENTS,
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
    assert rows[0][5] == "BTCUSDT"
    assert rows[0][6] == "ticker"
    assert "payload_json" in columns
    assert repo._last_event_time == event.event_time.isoformat()


def test_live_persistence_schema_contains_required_contracts():
    assert "event_time DateTime64(3, 'UTC')" in CREATE_MARKET_EVENTS
    assert "ingest_time DateTime64(3, 'UTC')" in CREATE_MARKET_EVENTS
    assert "payload_json String" in CREATE_MARKET_EVENTS
    assert "category LowCardinality(String)" in CREATE_LIVE_ANALYTICS_EVENTS
    assert "source_module LowCardinality(String)" in CREATE_LIVE_ANALYTICS_EVENTS
    assert "payload_json String" in CREATE_LIVE_ANALYTICS_EVENTS
