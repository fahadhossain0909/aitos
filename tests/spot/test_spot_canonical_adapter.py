from datetime import datetime, timezone

import pytest

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.market_data.binance_spot_adapter import BinanceSpotCanonicalMarketDataAdapter
from aitos.market_data.bus import (
    channel_for,
    market_event_from_wire,
    market_event_to_wire,
)
from aitos.market_data.channels import (
    ALL_CHANNELS,
    CHANNEL_KLINE,
    CHANNEL_SPOT_KLINE,
    CHANNEL_SPOT_TRADE,
)
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

    async def stream_many(self, streams):
        for stream in streams:
            symbol = stream.split("@", 1)[0].upper()
            channel = stream[len(symbol) :]
            async for event in self.stream(symbol, channel):
                yield {"stream": stream, "data": event["data"]}

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
        elif channels == ("@ticker",):
            yield {
                "data": {
                    "E": 1700000000000,
                    "s": symbol,
                    "c": "100",
                    "q": "500000",
                    "P": "1.5",
                    "b": "99.5",
                    "B": "2",
                    "a": "100.5",
                    "A": "3",
                }
            }
        elif channels == ("@kline_1m",):
            for last_trade_id in (20, 21):
                yield {
                    "data": {
                        "k": {
                            "t": 1699999980000,
                            "T": 1700000039999,
                            "L": last_trade_id,
                            "x": True,
                            "o": "99",
                            "h": "102",
                            "l": "98",
                            "c": "101",
                            "v": "10",
                            "q": "1000",
                            "n": 20,
                            "V": "5",
                            "Q": "500",
                        }
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
async def test_spot_ticker_stream_uses_exchange_timestamps():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket())
    events = [event async for event in adapter.stream_tickers(["BTCUSDT"])]
    assert len(events) == 1
    assert events[0].event_type is MarketEventType.TICKER
    assert events[0].payload["bid_price"] == "99.5"
    assert events[0].payload["ask_price"] == "100.5"
    assert events[0].payload["last_price"] == "100"
    assert events[0].event_time == datetime.fromtimestamp(1700000000, timezone.utc)


def test_spot_kline_uses_registered_canonical_event_channel():
    assert channel_for(MarketEventType.KLINE) == CHANNEL_KLINE
    assert CHANNEL_KLINE in ALL_CHANNELS
    assert channel_for(MarketEventType.KLINE, market_type="spot") == CHANNEL_SPOT_KLINE
    assert channel_for(MarketEventType.TRADE, market_type="spot") == CHANNEL_SPOT_TRADE
    assert CHANNEL_SPOT_KLINE in ALL_CHANNELS
    assert CHANNEL_SPOT_TRADE in ALL_CHANNELS


@pytest.mark.asyncio
async def test_spot_events_never_enter_futures_live_scanner_cache():
    from aitos.market_data.binance_spot_adapter import _event

    cache = LiveScannerCache(None, ["BTCUSDT"])
    event = _event(
        "BTCUSDT",
        MarketEventType.TRADE,
        datetime.now(timezone.utc),
        {"trade_id": 1, "price": "100", "quantity": "1", "buyer_is_maker": False},
        MarketSource.WEBSOCKET,
        1,
    )
    await cache._on_trade_event(event)
    assert cache.snapshot("BTCUSDT") is None


@pytest.mark.asyncio
async def test_spot_closed_kline_has_stable_identity_and_increasing_revision():
    adapter = BinanceSpotCanonicalMarketDataAdapter(FakeRest(), FakeWebSocket())
    events = [
        event async for event in adapter.stream_klines(["BTCUSDT"], timeframe="1m")
    ]
    assert len(events) == 2
    assert all(event.event_type is MarketEventType.KLINE for event in events)
    assert events[0].event_id == events[1].event_id
    assert [event.event_revision for event in events] == [20, 21]
    assert events[0].instrument_id == "binance:spot:BTCUSDT"
