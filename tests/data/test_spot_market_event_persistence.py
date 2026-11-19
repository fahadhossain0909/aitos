from datetime import datetime, timezone

import pytest

from aitos.core.contracts import Event
from aitos.data.market_os_persistence import MarketOSPersistence


@pytest.mark.asyncio
async def test_spot_market_event_handler_preserves_market_scoped_identity():
    persistence = MarketOSPersistence(event_bus=object(), repository=None)
    captured = {}

    async def capture(table, row, event):
        captured.update(table=table, row=row, event=event)

    persistence._enqueue = capture
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_type": "ticker",
        "exchange": "binance",
        "venue": "binance",
        "market": "spot",
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "instrument_id": "binance:spot:BTCUSDT",
        "event_time": now,
        "ingest_time": now,
        "source": "websocket",
        "event_id": "spot-event-1",
        "sequence": 123,
        "schema_version": 1,
        "correlation_id": "spot-correlation-1",
        "trace_id": "md:BTCUSDT:spot-event-1",
        "payload": {"last_price": "100"},
    }
    event = Event(
        topic="market.spot.ticker.BTCUSDT",
        payload=payload,
        event_id="spot-event-1",
        correlation_id="spot-correlation-1",
    )

    await persistence._handle_spot_market_event(event)

    assert captured["table"] == "market_events"
    row = captured["row"]
    assert row[2] == "spot-event-1"
    assert row[6] == "spot"
    assert row[7] == "binance:spot:BTCUSDT"
    assert row[8] == "BTCUSDT"
    assert row[9] == "ticker"
    assert row[11] == 123
    assert row[15] == '{"last_price": "100"}'


@pytest.mark.asyncio
async def test_spot_channel_rejects_non_spot_payload():
    persistence = MarketOSPersistence(event_bus=object(), repository=None)
    event = Event(
        topic="market.spot.ticker.BTCUSDT",
        payload={
            "event_time": datetime.now(timezone.utc).isoformat(),
            "ingest_time": datetime.now(timezone.utc).isoformat(),
            "market_type": "usd_m_futures",
            "symbol": "BTCUSDT",
        },
    )
    with pytest.raises(ValueError, match="non-Spot"):
        await persistence._handle_spot_market_event(event)
