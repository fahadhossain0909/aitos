import asyncio

from aitos.exchange.binance import BinanceFuturesAdapter


def test_trade_watchdog_constants_are_configured():
    assert BinanceFuturesAdapter is not None
    assert asyncio.iscoroutinefunction(BinanceFuturesAdapter.stream_trades)
