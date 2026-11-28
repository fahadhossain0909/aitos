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
    gateway = MarketDataGateway("binance", "usd_m_futures", lambda _event: asyncio.sleep(0))
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
