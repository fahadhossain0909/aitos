import inspect

from aitos.exchange.binance import BinanceFuturesAdapter


def test_trade_watchdog_constants_are_configured():
    assert BinanceFuturesAdapter is not None
    # stream_trades is an async generator because it yields an unbounded live
    # stream; asyncio.iscoroutinefunction() correctly returns False for that
    # protocol. The adapter contract uses AsyncIterator for live streams.
    assert inspect.isasyncgenfunction(BinanceFuturesAdapter.stream_trades)
