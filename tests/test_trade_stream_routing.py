import asyncio
import json

import pytest

from aitos.exchange.binance import WS_MARKET_BASE_URL, BinanceFuturesAdapter


class FakeWS:
    def __init__(self, messages):
        self.messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_stream_trades_uses_futures_combined_aggtrade_stream():
    urls = []

    def connector(url):
        urls.append(url)
        return FakeWS(
            [
                json.dumps(
                    {
                        "stream": "btcusdt@aggTrade",
                        "data": {
                            "e": "aggTrade",
                            "E": 1700000000000,
                            "s": "BTCUSDT",
                            "a": 1,
                            "p": "50000.0",
                            "q": "0.01",
                            "f": 10,
                            "l": 10,
                            "T": 1700000000000,
                            "m": True,
                        },
                    }
                )
            ]
        )

    adapter = BinanceFuturesAdapter(ws_connector=connector)
    stream = adapter.stream_trades(["BTCUSDT", "ETHUSDT"])
    trade = await anext(stream)
    await asyncio.sleep(0.01)

    assert trade.symbol == "BTCUSDT"
    assert trade.price == 50000.0
    assert trade.quantity == 0.01
    assert urls == [f"{WS_MARKET_BASE_URL}?streams=btcusdt@aggTrade/ethusdt@aggTrade"]

    await stream.aclose()
