from datetime import datetime, timezone

import pytest

from aitos.market_data.binance_spot_adapter import BinanceSpotCanonicalMarketDataAdapter
from aitos.market_data.bus import market_event_from_wire, market_event_to_wire
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.market_data.venues import InstrumentKey, MarketType, Venue
from aitos.spot.websocket import SpotOrderBookGap


class FakeRest:
    testnet = True

    async def depth(self, symbol, limit=100):
        return {"lastUpdateId": 10, "bids": [["99", "2"]], "asks": [["101", "3"]]}

    async def close(self):
        return None


class FakeWebSocket:
    def __init__(self, gap=False):
        self.gap = gap

    async def stream(self, symbol, *channels):
        if channels == ("@trade",):
            yield {
                "data": {
                    "T": 1700000000000,
                    "t": 7,
                    "p": "100",
                    "q": "0.25",
                    "m": False,
                }
            }
        elif channels == ("@bookTicker",):
            yield {
                "data": {
                    "u": 99,
                    "s": symbol,
                    "b": "99.5",
                    "B": "2",
                    "a": "100.5",
                    "A": "3",
                }
            }
        elif channels == ("@depth@100ms",):
            first = 14 if self.gap else 11
            yield {
                "data": {
                    "E": 1700000000100,
                    "U": first,
                    "u": first + 1,
                    "b": [["99", "1"]],
                    "a": [["101", "4"]],
                }
            }


@pytest.mark.asyncio
async def test_spot_adapter_emits_canonical_market_identity_and_trade_events():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket())
    events = [event async for event in adapter.stream_trades(["btcusdt", "ethusdt"])]
    assert {event.symbol for event in events} == {"BTCUSDT", "ETHUSDT"}
    assert all(event.market_type == MarketType.SPOT.value for event in events)
    assert all(
        event.instrument_id
        == InstrumentKey(Venue.BINANCE, MarketType.SPOT, event.symbol).value
        for event in events
    )
    assert all(event.event_type is MarketEventType.TRADE for event in events)


@pytest.mark.asyncio
async def test_spot_orderbook_bootstraps_snapshot_then_sequence_checked_delta():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket())
    events = [
        event async for event in adapter.stream_order_books(["BTCUSDT"], levels=20)
    ]
    assert [event.event_type for event in events] == [
        MarketEventType.BOOK_SNAPSHOT,
        MarketEventType.BOOK_DELTA,
    ]
    assert events[0].source is MarketSource.REST
    assert events[0].payload["last_update_id"] == 10
    assert events[1].payload["first_update_id"] == 11


@pytest.mark.asyncio
async def test_spot_orderbook_fails_closed_on_snapshot_sequence_gap():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket(gap=True))
    with pytest.raises(SpotOrderBookGap):
        _ = [
            event async for event in adapter.stream_order_books(["BTCUSDT"], levels=20)
        ]


def test_spot_market_event_round_trip_preserves_market_type_and_identity():
    from aitos.market_data.binance_spot_adapter import _event

    event = _event(
        "BTCUSDT",
        MarketEventType.TRADE,
        datetime.now(timezone.utc),
        {"price": "100"},
        MarketSource.REPLAY,
        3,
    )
    restored = market_event_from_wire(market_event_to_wire(event))
    assert restored.market_type == "spot"
    assert restored.instrument_id == "binance:spot:BTCUSDT"
    assert restored.sequence == 3


@pytest.mark.asyncio
async def test_spot_ticker_stream_handles_book_ticker_without_exchange_time():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket())
    events = [event async for event in adapter.stream_tickers(["BTCUSDT"])]
    assert len(events) == 1
    assert events[0].event_type is MarketEventType.TICKER
    assert events[0].payload["bid_price"] == "99.5"
    assert events[0].payload["ask_price"] == "100.5"
