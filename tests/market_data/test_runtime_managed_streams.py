import asyncio
from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import MarketDataGateway
from aitos.market_data.runtime import CanonicalMarketDataRuntime
from aitos.market_data.venues import MarketType, Venue, VenueCapabilities


class _FakeManagedStream:
    def __init__(self, symbols, event_type, make_payload):
        self.symbols = list(symbols)
        self._event_type = event_type
        self._make_payload = make_payload
        self.update_calls: list[list[str]] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tick = 0
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(0.01)
            self._tick += 1
            if not self.symbols:
                continue
            symbol = self.symbols[0]
            await self._queue.put(
                MarketEvent(
                    event_type=self._event_type,
                    exchange="binance",
                    market="usd_m_futures",
                    symbol=symbol,
                    event_time=datetime.now(timezone.utc),
                    payload=self._make_payload(self._tick),
                    source=MarketSource.WEBSOCKET,
                )
            )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()

    async def update_symbols(self, symbols):
        self.symbols = list(symbols)
        self.update_calls.append(list(symbols))
        return True

    async def aclose(self):
        self.closed = True
        self._pump_task.cancel()
        await asyncio.gather(self._pump_task, return_exceptions=True)


class ManagedFakeAdapter:
    venue = Venue.BINANCE
    market_type = MarketType.USD_M_FUTURES
    capabilities = VenueCapabilities(trades=True, order_book=True)

    def __init__(self) -> None:
        self.trade_stream_creations = 0
        self.book_stream_creations = 0
        self.trade_streams: list[_FakeManagedStream] = []
        self.book_streams: list[_FakeManagedStream] = []

    async def stream_trades_managed(self, symbols):
        self.trade_stream_creations += 1
        stream = _FakeManagedStream(
            symbols, MarketEventType.TRADE, lambda tick: {"trade_id": tick}
        )
        self.trade_streams.append(stream)
        return stream

    async def stream_order_books_managed(self, symbols, levels=20):
        self.book_stream_creations += 1
        stream = _FakeManagedStream(
            symbols,
            MarketEventType.BOOK_SNAPSHOT,
            lambda tick: {"bids": [], "asks": [], "last_update_id": tick},
        )
        self.book_streams.append(stream)
        return stream


class NoopBus:
    async def publish(self, event):
        return None


@pytest.mark.asyncio
async def test_runtime_updates_orderbook_symbols_without_reconnect_when_managed():
    adapter = ManagedFakeAdapter()
    gateway = MarketDataGateway(
        venue="binance", market_type="usd_m_futures", publisher=NoopBus().publish
    )
    runtime = CanonicalMarketDataRuntime(
        adapter=adapter,
        market_bus=NoopBus(),
        gateway=gateway,
        symbols=["BTCUSDT"],
        orderbook_symbols=["BTCUSDT"],
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    changed = await runtime.update_orderbook_symbols(["BTCUSDT", "ETHUSDT"])
    await asyncio.sleep(0.05)
    await runtime.stop()

    assert changed is True
    # Exactly one connection was ever created for the orderbook stream --
    # the symbol change was handled as an incremental update, not a restart.
    assert adapter.book_stream_creations == 1
    assert adapter.book_streams[0].update_calls == [["BTCUSDT", "ETHUSDT"]]
    assert adapter.book_streams[0].closed is True


@pytest.mark.asyncio
async def test_runtime_updates_trade_symbols_without_reconnect_when_managed():
    adapter = ManagedFakeAdapter()
    gateway = MarketDataGateway(
        venue="binance", market_type="usd_m_futures", publisher=NoopBus().publish
    )
    runtime = CanonicalMarketDataRuntime(
        adapter=adapter,
        market_bus=NoopBus(),
        gateway=gateway,
        symbols=["BTCUSDT"],
        enable_orderbooks=False,
    )

    await runtime.start()
    await asyncio.sleep(0.05)
    changed = await runtime.update_trade_symbols(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    await asyncio.sleep(0.05)
    await runtime.stop()

    assert changed is True
    assert adapter.trade_stream_creations == 1
    assert adapter.trade_streams[0].update_calls == [["BTCUSDT", "ETHUSDT", "SOLUSDT"]]
