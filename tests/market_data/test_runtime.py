import asyncio
from datetime import datetime, timezone

import pytest

from aitos.market_data.adapter import CanonicalMarketDataAdapter
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import MarketDataGateway
from aitos.market_data.runtime import CanonicalMarketDataRuntime
from aitos.market_data.venues import MarketType, Venue, VenueCapabilities


class FakeAdapter:
    venue = Venue.BINANCE
    market_type = MarketType.USD_M_FUTURES
    capabilities = VenueCapabilities(trades=True, order_book=True)

    def __init__(self) -> None:
        self.trade_starts = 0
        self.book_starts = 0
        self.book_symbols = []

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
        self.book_symbols.append(list(symbols))
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


def test_fake_adapter_conforms_to_canonical_protocol():
    assert isinstance(FakeAdapter(), CanonicalMarketDataAdapter)


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
    await asyncio.sleep(1.2)
    await runtime.stop()

    assert len(published) >= 2
    assert adapter.trade_starts >= 2
    assert adapter.book_starts >= 2
    assert gateway.health.reconnect_count >= 2
    assert gateway.state.value == "stopped"


@pytest.mark.asyncio
async def test_runtime_hot_switches_orderbook_symbols():
    adapter = FakeAdapter()
    gateway = MarketDataGateway(
        venue="binance",
        market_type="usd_m_futures",
        publisher=NoopBus().publish,
    )
    runtime = CanonicalMarketDataRuntime(
        adapter=adapter,
        market_bus=NoopBus(),
        gateway=gateway,
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        orderbook_symbols=["BTCUSDT"],
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    changed = await runtime.update_orderbook_symbols(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    await asyncio.sleep(0.05)
    await runtime.stop()

    assert changed is True
    assert runtime.orderbook_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert adapter.book_starts >= 2
    assert adapter.book_symbols[0] == ["BTCUSDT"]
    assert adapter.book_symbols[-1] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
