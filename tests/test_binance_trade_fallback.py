import pytest

import aitos.exchange.binance as binance_module
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.models.market import TradeSide


def _agg_payload(symbol: str, trade_id: int = 1) -> dict:
    return {
        "e": "aggTrade",
        "E": 1_700_000_000_000,
        "s": symbol,
        "a": trade_id,
        "p": "100.0",
        "q": "1.0",
        "f": trade_id,
        "l": trade_id,
        "T": 1_700_000_000_000,
        "m": False,
    }


@pytest.mark.asyncio
async def test_trade_stream_uses_bounded_combined_stream(monkeypatch):
    calls = []

    async def fake_raw_stream(streams, emit_reconnect=False):
        calls.append((streams, emit_reconnect))
        yield _agg_payload("BTCUSDT", trade_id=2), streams[0]

    adapter = BinanceFuturesAdapter()
    monkeypatch.setattr(adapter, "_raw_stream", fake_raw_stream)

    tick = await anext(adapter.stream_trades(["btcusdt", "BTCUSDT"]))

    assert tick.symbol == "BTCUSDT"
    assert tick.trade_id == 2
    assert tick.side is TradeSide.BUY
    assert calls == [(["btcusdt@aggTrade"], True)]


@pytest.mark.asyncio
async def test_empty_trade_symbols_do_not_open_websocket(monkeypatch):
    called = False

    async def fake_raw_stream(streams, emit_reconnect=False):
        nonlocal called
        called = True
        yield {}, ""

    adapter = BinanceFuturesAdapter()
    monkeypatch.setattr(adapter, "_raw_stream", fake_raw_stream)

    stream = adapter.stream_trades([])
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert called is False


def test_trade_stream_no_longer_exposes_per_symbol_fallback():
    assert not hasattr(BinanceFuturesAdapter, "_direct_raw_stream")
    assert not hasattr(binance_module, "TRADE_STREAM_IDLE_FALLBACK_SECONDS")
    assert not hasattr(binance_module, "TRADE_STREAM_PRIMARY_RETRY_SECONDS")


def test_trade_stream_connection_bound_for_large_universe():
    streams = [f"symbol{i}@aggTrade" for i in range(848)]
    shards = BinanceFuturesAdapter._partition_streams(streams)

    assert len(shards) == 5
    assert [len(shard) for shard in shards] == [200, 200, 200, 200, 48]
    assert all(
        len(shard) <= binance_module.BINANCE_MAX_STREAMS_PER_CONNECTION
        for shard in shards
    )
