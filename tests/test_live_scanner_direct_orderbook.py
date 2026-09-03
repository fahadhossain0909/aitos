from datetime import datetime, timezone

import pytest

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


@pytest.mark.asyncio
async def test_direct_orderbook_updates_scanner_cache(event_bus):
    cache = LiveScannerCache(event_bus, ["BTCUSDT"])
    await cache.initialize(direct_market_data=True)
    now = datetime.now(timezone.utc)
    book = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 1.0),),
        asks=((101.0, 1.0),),
        last_update_id=456,
        timestamp=now,
    )
    trade = TradeTick(
        symbol="BTCUSDT",
        trade_id=123,
        price=100.5,
        quantity=1.0,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=now,
    )

    await cache.accept_live_order_book(book)
    await cache.accept_live_trade(trade)

    state = cache.snapshot("BTCUSDT")
    assert state is not None
    assert state.order_book == book
    assert state.last_book_source_at == now
    assert state.last_trade_source_at == now
    assert cache.is_book_fresh("BTCUSDT", 5.0)
    assert cache.is_trade_fresh("BTCUSDT", 5.0)
    await cache.shutdown()
