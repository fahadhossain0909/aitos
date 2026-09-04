from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


class FakeSubscription:
    def cancel(self):
        pass


class FakeBus:
    def __init__(self):
        self.subscriptions = []

    async def subscribe(
        self, topic, handler, group="default", start_id="$", live_only=False
    ):
        self.subscriptions.append((topic, group, handler, start_id, live_only))
        return FakeSubscription()


def trade_event(trade, source=MarketSource.WEBSOCKET, event_time=None):
    return type(
        "Event",
        (),
        {
            "event_type": MarketEventType.TRADE,
            "symbol": trade.symbol,
            "payload": trade.to_dict(),
            "source": source,
            "event_time": event_time or trade.timestamp,
        },
    )()


def book_event(book, source=MarketSource.WEBSOCKET, event_time=None):
    return type(
        "Event",
        (),
        {
            "event_type": MarketEventType.BOOK_SNAPSHOT,
            "symbol": book.symbol,
            "payload": book.to_dict(),
            "source": source,
            "event_time": event_time or book.timestamp,
        },
    )()


@pytest.mark.asyncio
async def test_initialize_subscribes_once_to_canonical_live_channels():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=100)
    await cache.initialize(direct_market_data=True)

    topics = [item[0] for item in bus.subscriptions]
    assert topics == ["market.trade", "market.book.snapshot"]
    assert all(item[1] == "live-scanner-cache-v1" for item in bus.subscriptions)
    assert all(item[4] is True for item in bus.subscriptions)
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_accepts_fresh_websocket_trade_and_book():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    ts = datetime.now(timezone.utc)
    trade = TradeTick("BTCUSDT", 1, 100.0, 2.0, TradeSide.BUY, False, ts)
    book = OrderBookSnapshot("BTCUSDT", ((99.0, 5.0),), ((101.0, 4.0),), 7, ts)

    await cache._on_trade_event(trade_event(trade))
    await cache._on_book_event(book_event(book))

    snapshot = cache.snapshot("BTCUSDT")
    assert snapshot is not None
    assert list(snapshot.trades) == [trade]
    assert snapshot.order_book == book
    assert snapshot.last_trade_at is not None
    assert snapshot.last_book_at is not None
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_rejects_stale_trade_and_book():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    stale_ts = datetime.now(timezone.utc) - timedelta(seconds=120)
    trade = TradeTick("BTCUSDT", 1, 100.0, 2.0, TradeSide.BUY, False, stale_ts)
    book = OrderBookSnapshot("BTCUSDT", ((99.0, 5.0),), ((101.0, 4.0),), 7, stale_ts)

    await cache._on_trade_event(trade_event(trade))
    await cache._on_book_event(book_event(book))

    snapshot = cache.snapshot("BTCUSDT")
    assert snapshot is not None
    assert list(snapshot.trades) == []
    assert snapshot.order_book is None
    assert snapshot.stale_trade_rejections == 1
    assert snapshot.stale_book_rejections == 1
    assert snapshot.last_stale_trade_source_age_sec >= 120.0
    assert snapshot.last_stale_book_source_age_sec >= 120.0
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_rejects_rest_events_from_live_state():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    ts = datetime.now(timezone.utc)
    trade = TradeTick("BTCUSDT", 1, 100.0, 2.0, TradeSide.BUY, False, ts)
    book = OrderBookSnapshot("BTCUSDT", ((99.0, 5.0),), ((101.0, 4.0),), 7, ts)

    await cache._on_trade_event(trade_event(trade, source=MarketSource.REST))
    await cache._on_book_event(book_event(book, source=MarketSource.REST))

    snapshot = cache.snapshot("BTCUSDT")
    assert snapshot is not None
    assert list(snapshot.trades) == []
    assert snapshot.order_book is None
    assert snapshot.stale_trade_rejections == 1
    assert snapshot.stale_book_rejections == 1
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_deduplicates_trades_and_books():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    ts = datetime.now(timezone.utc)
    trade = TradeTick("BTCUSDT", 42, 100.0, 2.0, TradeSide.BUY, False, ts)
    book = OrderBookSnapshot("BTCUSDT", ((99.0, 5.0),), ((101.0, 4.0),), 7, ts)

    await cache._on_trade_event(trade_event(trade))
    await cache._on_trade_event(trade_event(trade))
    await cache._on_book_event(book_event(book))
    await cache._on_book_event(book_event(book))

    snapshot = cache.snapshot("BTCUSDT")
    assert snapshot is not None
    assert list(snapshot.trades) == [trade]
    assert snapshot.order_book == book
    assert snapshot.duplicate_trade_rejections == 1
    assert snapshot.duplicate_book_rejections == 1
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_filters_symbols():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    ts = datetime.now(timezone.utc)
    trade = TradeTick("ETHUSDT", 1, 100.0, 2.0, TradeSide.BUY, False, ts)

    await cache._on_trade_event(trade_event(trade))

    assert cache.snapshot("ETHUSDT") is None
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_keeps_bounded_trade_history():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=100)
    ts = datetime.now(timezone.utc)
    for i in range(150):
        trade = TradeTick("BTCUSDT", i, 100.0 + i, 1.0, TradeSide.BUY, False, ts)
        await cache._on_trade_event(trade_event(trade, event_time=ts))

    snapshot = cache.snapshot("BTCUSDT")
    assert snapshot is not None
    assert len(snapshot.trades) == 100
