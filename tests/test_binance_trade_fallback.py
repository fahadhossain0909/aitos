import asyncio

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
async def test_silent_primary_triggers_fallback_without_blocking(monkeypatch):
    monkeypatch.setattr(binance_module, "TRADE_STREAM_IDLE_FALLBACK_SECONDS", 0.05)
    monkeypatch.setattr(binance_module, "TRADE_STREAM_PRIMARY_RETRY_SECONDS", 0.01)

    async def fake_raw_stream(streams, emit_reconnect=False):
        stream = streams[0]
        await asyncio.sleep(10)
        return
        yield _agg_payload("BTCUSDT"), stream  # pragma: no cover

    async def fake_direct_raw_stream(stream, emit_reconnect=False):
        yield _agg_payload("BTCUSDT", trade_id=2), stream
        await asyncio.sleep(10)

    adapter = BinanceFuturesAdapter()
    adapter._raw_stream = fake_raw_stream
    adapter._direct_raw_stream = fake_direct_raw_stream

    tick = await asyncio.wait_for(
        anext(adapter.stream_trades(["BTCUSDT"])), timeout=0.5
    )

    assert tick.symbol == "BTCUSDT"
    assert tick.trade_id == 2
    assert tick.side is TradeSide.BUY


@pytest.mark.asyncio
async def test_primary_recovery_immediately_wins_after_fallback(monkeypatch):
    monkeypatch.setattr(binance_module, "TRADE_STREAM_IDLE_FALLBACK_SECONDS", 0.05)
    monkeypatch.setattr(binance_module, "TRADE_STREAM_PRIMARY_RETRY_SECONDS", 0.01)

    async def fake_raw_stream(streams, emit_reconnect=False):
        stream = streams[0]
        await asyncio.sleep(0.08)
        yield _agg_payload("ETHUSDT", trade_id=20), stream
        await asyncio.sleep(10)

    async def fake_direct_raw_stream(stream, emit_reconnect=False):
        yield _agg_payload("ETHUSDT", trade_id=10), stream
        await asyncio.sleep(10)

    adapter = BinanceFuturesAdapter()
    adapter._raw_stream = fake_raw_stream
    adapter._direct_raw_stream = fake_direct_raw_stream
    stream = adapter.stream_trades(["ETHUSDT"])

    first = await asyncio.wait_for(anext(stream), timeout=0.5)
    second = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert first.trade_id == 10
    assert second.trade_id == 20
    assert second.side is TradeSide.BUY

    await stream.aclose()


@pytest.mark.asyncio
async def test_primary_wins_when_recovery_and_fallback_are_ready_together(monkeypatch):
    monkeypatch.setattr(binance_module, "TRADE_STREAM_IDLE_FALLBACK_SECONDS", 0.05)
    monkeypatch.setattr(binance_module, "TRADE_STREAM_PRIMARY_RETRY_SECONDS", 0.01)

    async def fake_raw_stream(streams, emit_reconnect=False):
        stream = streams[0]
        await asyncio.sleep(0.06)
        yield _agg_payload("SOLUSDT", trade_id=20), stream
        await asyncio.sleep(10)

    async def fake_direct_raw_stream(stream, emit_reconnect=False):
        await asyncio.sleep(0.06)
        yield _agg_payload("SOLUSDT", trade_id=10), stream
        await asyncio.sleep(10)

    adapter = BinanceFuturesAdapter()
    adapter._raw_stream = fake_raw_stream
    adapter._direct_raw_stream = fake_direct_raw_stream
    stream = adapter.stream_trades(["SOLUSDT"])

    tick = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert tick.trade_id == 20
    assert tick.side is TradeSide.BUY

    await stream.aclose()
