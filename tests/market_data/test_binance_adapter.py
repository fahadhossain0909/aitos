from datetime import datetime, timezone

import pytest

from aitos.market_data.binance_adapter import BinanceCanonicalMarketDataAdapter
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.models.market import OrderBookSnapshot, TradeTick


class FakeExchange:
    async def stream_trades(self, symbols):
        yield TradeTick(
            symbol=symbols[0],
            price=100.0,
            quantity=1.0,
            timestamp=datetime.now(timezone.utc),
            trade_id=1,
            is_buyer_maker=False,
        )

    async def stream_order_book(self, symbols, levels=20):
        yield OrderBookSnapshot(
            symbol=symbols[0],
            bids=[(99.0, 2.0)],
            asks=[(101.0, 2.0)],
            timestamp=datetime.now(timezone.utc),
            last_update_id=7,
        )

    async def fetch_recent_trades(self, symbol, limit=500):
        return []

    async def fetch_order_book(self, symbol, limit=50):
        return OrderBookSnapshot(
            symbol=symbol,
            bids=[(99.0, 2.0)],
            asks=[(101.0, 2.0)],
            timestamp=datetime.now(timezone.utc),
            last_update_id=8,
        )


@pytest.mark.asyncio
async def test_trade_stream_is_canonical():
    adapter = BinanceCanonicalMarketDataAdapter(FakeExchange())
    event = await anext(adapter.stream_trades(["BTCUSDT"]))
    assert event.event_type is MarketEventType.TRADE
    assert event.source is MarketSource.WEBSOCKET
    assert event.market_type == "usd_m_futures"
    assert event.venue == "binance"


@pytest.mark.asyncio
async def test_book_stream_is_canonical():
    adapter = BinanceCanonicalMarketDataAdapter(FakeExchange())
    event = await anext(adapter.stream_order_books(["BTCUSDT"]))
    assert event.event_type is MarketEventType.BOOK_SNAPSHOT
    assert event.source is MarketSource.WEBSOCKET


@pytest.mark.asyncio
async def test_rest_recovery_is_explicitly_rest():
    adapter = BinanceCanonicalMarketDataAdapter(FakeExchange())
    event = await adapter.recover_order_book("BTCUSDT")
    assert event.source is MarketSource.REST
