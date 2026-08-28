from datetime import datetime, timezone

import pytest

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


class FakeSubscription:
    def __init__(self, handler):
        self.handler = handler

    def cancel(self):
        pass


class FakeBus:
    def __init__(self):
        self.subscriptions = []

    async def subscribe(self, topic, handler, group="default", start_id=None):
        self.subscriptions.append((topic, group, handler, start_id))
        return FakeSubscription(handler)


@pytest.mark.asyncio
async def test_live_cache_rehydrates_trade_and_book_events():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=10)
    await cache.initialize()
    ts = datetime.now(timezone.utc)
    trade = TradeTick("BTCUSDT", 1, 100.0, 2.0, TradeSide.BUY, False, ts)
    book = OrderBookSnapshot("BTCUSDT", ((99.0, 5.0),), ((101.0, 4.0),), 7, ts)
    await cache._on_trade(
        type("E", (), {"payload": trade.to_dict(), "topic": "market.trade.BTCUSDT"})()
    )
    await cache._on_book(
        type(
            "E", (), {"payload": book.to_dict(), "topic": "market.orderbook.BTCUSDT"}
        )()
    )
    assert cache.recent_trades("BTCUSDT") == [trade]
    assert cache.order_book("BTCUSDT") == book
    await cache.shutdown()


@pytest.mark.asyncio
async def test_live_cache_keeps_bounded_trade_history():
    bus = FakeBus()
    cache = LiveScannerCache(bus, ["BTCUSDT"], max_trades=100)
    ts = datetime.now(timezone.utc)
    for i in range(150):
        trade = TradeTick("BTCUSDT", i, 100.0 + i, 1.0, TradeSide.BUY, False, ts)
        await cache._on_trade(
            type(
                "E", (), {"payload": trade.to_dict(), "topic": "market.trade.BTCUSDT"}
            )()
        )
    assert len(cache.recent_trades("BTCUSDT")) == 100
    assert cache.recent_trades("BTCUSDT")[0].trade_id == 50
