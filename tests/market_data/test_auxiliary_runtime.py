import asyncio

import pytest

from aitos.market_data.auxiliary_runtime import BinanceAuxiliaryMarketDataRuntime
from aitos.market_data.gateway import MarketDataGateway


class _NeverEndingAdapter:
    async def _stream(self, *args, **kwargs):
        while True:
            await asyncio.sleep(10)
            yield None

    def stream_all_tickers(self):
        return self._stream()

    def stream_funding(self, symbols):
        return self._stream(symbols)

    def stream_open_interest(self, symbols):
        return self._stream(symbols)

    def stream_liquidations(self):
        return self._stream()

    def stream_instruments(self, symbols):
        return self._stream(symbols)


@pytest.mark.asyncio
async def test_instrument_poller_remains_active_after_symbol_rotation():
    adapter = _NeverEndingAdapter()
    gateway = MarketDataGateway(
        "binance", "usd_m_futures", lambda _event: asyncio.sleep(0)
    )
    runtime = BinanceAuxiliaryMarketDataRuntime(adapter, gateway, ["BTCUSDT"])

    await runtime.start()
    try:
        await asyncio.sleep(0)
        assert await runtime.update_symbols(["BTCUSDT", "ETHUSDT"]) is True
        await asyncio.sleep(0)
        producers = runtime.snapshot()["active_producers"]
        assert "market-data-instrument" in producers
        assert "market-data-funding" in producers
        assert "market-data-open_interest" in producers
    finally:
        await runtime.stop()


class _ManagedFunding:
    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.closed = False

    def __aiter__(self):
        return self._stream()

    async def _stream(self):
        while not self.closed:
            await asyncio.sleep(10)
            yield None

    async def update_symbols(self, symbols):
        self.symbols = list(symbols)
        return True

    async def aclose(self):
        self.closed = True


class _DynamicAdapter(_NeverEndingAdapter):
    def __init__(self):
        self.funding = None

    def stream_funding_managed(self, symbols):
        self.funding = _ManagedFunding(symbols)
        return self.funding

    def stream_open_interest_dynamic(self, symbol_provider):
        return self._stream(symbol_provider)

    def stream_instruments_dynamic(self, symbol_provider):
        return self._stream(symbol_provider)


@pytest.mark.asyncio
async def test_symbol_rotation_updates_managed_feeds_without_restarting_producers():
    adapter = _DynamicAdapter()
    gateway = MarketDataGateway("binance", "usd_m_futures", lambda _event: asyncio.sleep(0))
    runtime = BinanceAuxiliaryMarketDataRuntime(adapter, gateway, ["BTCUSDT"])

    await runtime.start()
    try:
        await asyncio.sleep(0)
        tasks_before = {task.get_name(): task for task in runtime._tasks}
        assert await runtime.update_symbols(["BTCUSDT", "ETHUSDT"]) is True
        await asyncio.sleep(0)

        tasks_after = {task.get_name(): task for task in runtime._tasks}
        assert tasks_after == tasks_before
        assert adapter.funding.symbols == ["BTCUSDT", "ETHUSDT"]
        assert "market-data-instrument" in runtime.snapshot()["active_producers"]
        assert "market-data-open_interest" in runtime.snapshot()["active_producers"]
    finally:
        await runtime.stop()
