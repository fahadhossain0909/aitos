from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import MarketDataGateway
from aitos.market_data.runtime import CanonicalMarketDataRuntime


class FakeAdapter:
    def __init__(self) -> None:
        self.trade_starts = 0
        self.book_starts = 0

    async def stream_trades(self, symbols):
        self.trade_starts += 1
        yield MarketEvent(
            event_type=MarketEventType.TRADE,
            exchange="binance",
            market="usd_m_futures",
            symbol=symbols[0],
            event_time=datetime.now(timezone.utc),
            payload={"trade_id": self.trade_starts},
            source=MarketSource.WEBSOCKET,
        )

    async def stream_order_books(self, symbols, levels):
        self.book_starts += 1
        yield MarketEvent(
            event_type=MarketEventType.BOOK_SNAPSHOT,
            exchange="binance",
            market="usd_m_futures",
            symbol=symbols[0],
            event_time=datetime.now(timezone.utc),
            payload={"bids": [], "asks": [], "last_update_id": self.book_starts},
            source=MarketSource.WEBSOCKET,
        )


class NoopBus:
    async def publish(self, event):
        return None


@pytest.mark.asyncio
async def test_runtime_restarts_streams_after_unexpected_end():
    adapter = FakeAdapter()
    published = []

    async def publisher(event):
        published.append(event)

    gateway = MarketDataGateway(
        venue="binance",
        market_type="usd_m_futures",
        publisher=publisher,
    )
    runtime = CanonicalMarketDataRuntime(
        adapter=adapter,
        market_bus=NoopBus(),
        gateway=gateway,
        symbols=["BTCUSDT"],
    )

    await runtime.start()
    await __import__("asyncio").sleep(1.2)
    await runtime.stop()

    assert len(published) >= 2
    assert adapter.trade_starts >= 2
    assert adapter.book_starts >= 2
    assert gateway.health.reconnect_count >= 2
    assert gateway.state.value == "stopped"
