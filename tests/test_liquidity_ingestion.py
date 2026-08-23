from datetime import datetime, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


class FakeExchange:
    async def connect(self):
        pass

    async def close(self):
        pass


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class FakeRepository:
    async def save_order_book_snapshot(self, book):
        pass

    async def save_trade_tick(self, trade):
        pass

    async def save_kline(self, kline):
        pass


def book(update_id, bid_qty, ask_qty):
    now = datetime.now(timezone.utc)
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, bid_qty),),
        asks=((101.0, ask_qty),),
        last_update_id=update_id,
        timestamp=now,
    )


def trade(tid, side):
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=tid,
        price=101.0 if side == TradeSide.BUY else 100.0,
        quantity=10.0,
        side=side,
        is_buyer_maker=side == TradeSide.SELL,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_live_orderbook_handler_publishes_liquidity_event():
    bus = FakeBus()
    service = DataIngestionService(
        FakeExchange(), bus, ["BTCUSDT"], repository=FakeRepository()
    )
    service._recent_trades["BTCUSDT"].append(trade(1, TradeSide.BUY))

    await service._handle_order_book(book(1, 100.0, 100.0))
    await service._handle_order_book(book(2, 100.0, 40.0))

    liquidity_events = [e for e in bus.events if e.topic == "market.liquidity.BTCUSDT"]
    assert liquidity_events
    assert any(e.payload["kind"] in {"pulling", "sweep"} for e in liquidity_events)


@pytest.mark.asyncio
async def test_orderbook_event_remains_published():
    bus = FakeBus()
    service = DataIngestionService(FakeExchange(), bus, ["BTCUSDT"])

    await service._handle_order_book(book(1, 100.0, 100.0))

    assert any(e.topic == "market.orderbook.BTCUSDT" for e in bus.events)
