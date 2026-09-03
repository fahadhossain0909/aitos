from datetime import datetime, timezone

from aitos.market_data.bus import (
    channel_for,
    market_event_from_wire,
    market_event_to_wire,
)
from aitos.market_data.channels import CHANNEL_BOOK_DELTA, CHANNEL_TRADE
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource


def test_channel_mapping_is_semantic():
    assert channel_for(MarketEventType.TRADE) == CHANNEL_TRADE
    assert channel_for(MarketEventType.BOOK_DELTA) == CHANNEL_BOOK_DELTA


def test_market_event_round_trip_preserves_identity_and_timing():
    source_ts = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    received_ts = datetime(2026, 9, 3, 10, 0, 0, 125000, tzinfo=timezone.utc)
    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        venue="binance",
        market="usd_m_futures",
        market_type="futures",
        symbol="BTCUSDT",
        instrument_id="BTCUSDT",
        event_time=source_ts,
        ingest_time=received_ts,
        source=MarketSource.WEBSOCKET,
        sequence=12345,
        correlation_id="corr-1",
        trace_id="trace-1",
        event_id="event-1",
        payload={"price": 100.0, "quantity": 0.5},
    )

    restored = market_event_from_wire(market_event_to_wire(event))

    assert restored == event
    assert restored.source_age_seconds == 0.125
    assert restored.source_ts == source_ts
    assert restored.received_ts == received_ts
