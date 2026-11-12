from datetime import datetime, timedelta, timezone

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


@pytest.mark.asyncio
async def test_depth_sequence_break_is_retained_as_amd_history_gap():
    cache = LiveScannerCache(None, ["BTCUSDT"])
    now = datetime.now(timezone.utc)
    timestamp_ms = int(now.timestamp() * 1000)
    first = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 1.0),),
        asks=((101.0, 1.0),),
        last_update_id=100,
        timestamp=now,
        depth_event={
            "event_time_ms": timestamp_ms,
            "first_update_id": 100,
            "final_update_id": 100,
            "previous_update_id": 99,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
            "reductions": [],
        },
    )
    broken = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 2.0),),
        asks=((101.0, 1.0),),
        last_update_id=102,
        timestamp=now + timedelta(milliseconds=1),
        depth_event={
            "event_time_ms": timestamp_ms + 1,
            "first_update_id": 102,
            "final_update_id": 102,
            "previous_update_id": 101,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
            "reductions": [],
        },
    )
    await cache.accept_live_order_book(first)
    await cache.accept_live_order_book(broken)
    state = cache.snapshot("BTCUSDT")
    assert state is not None
    assert state.depth_history_gap_at_ms == timestamp_ms + 1
    assert state.depth_event_history[-1]["sequence_gap"] is True
