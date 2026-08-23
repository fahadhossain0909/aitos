import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, List

import pytest

from aitos.exchange.base import ExchangeAdapter
from aitos.knowledge_graph.correlation_updater import SymbolCorrelationUpdater
from aitos.knowledge_graph.writer import KnowledgeGraphWriter
from aitos.models.market import (FundingRate, Kline, OpenInterest,
                                 OrderBookSnapshot)
from tests.test_knowledge_graph_writer import FakeDriver

NOW = datetime.now(timezone.utc)


def make_klines(closes):
    klines = []
    for i, close in enumerate(closes):
        t = NOW + timedelta(hours=i)
        klines.append(
            Kline(
                symbol="X",
                timeframe="1h",
                open_time=t,
                close_time=t + timedelta(hours=1),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100.0,
                quote_volume=100.0 * close,
                trades_count=10,
                taker_buy_volume=50.0,
                taker_buy_quote_volume=50.0 * close,
            )
        )
    return klines


class FakeCorrelationExchange(ExchangeAdapter):
    """BTCUSDT and ETHUSDT move in lockstep (perfectly correlated);
    XRPUSDT moves inversely (perfectly anti-correlated)."""

    def __init__(self):
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def fetch_klines(self, symbol, timeframe, limit=500) -> List[Kline]:
        base = [100.0 + i for i in range(30)]
        if symbol == "BTCUSDT":
            return make_klines(base)
        if symbol == "ETHUSDT":
            return make_klines(
                [b * 2 for b in base]
            )  # scaled but same direction -> correlation 1.0
        if symbol == "XRPUSDT":
            # Construct XRP's returns to be the exact negative of BTC's returns at
            # each step (multiplicatively) — this is the only way to guarantee an
            # exact -1.0 correlation in *returns* space; an additive price inverse
            # (e.g. 200 - base) does NOT produce anti-correlated returns.
            xrp = [100.0]
            for i in range(1, len(base)):
                btc_return = (base[i] - base[i - 1]) / base[i - 1]
                xrp.append(xrp[-1] * (1 - btc_return))
            return make_klines(xrp)
        return make_klines(base)

    async def fetch_order_book(self, symbol, limit=50) -> OrderBookSnapshot:
        raise NotImplementedError

    async def fetch_recent_trades(self, symbol, limit=500):
        raise NotImplementedError

    async def fetch_funding_rate(self, symbol) -> FundingRate:
        raise NotImplementedError

    async def fetch_open_interest(self, symbol) -> OpenInterest:
        raise NotImplementedError

    async def stream_klines(self, symbols, timeframe) -> AsyncIterator[Kline]:
        return
        yield  # pragma: no cover

    async def stream_trades(self, symbols) -> AsyncIterator:
        return
        yield  # pragma: no cover

    async def stream_order_book(self, symbols) -> AsyncIterator[OrderBookSnapshot]:
        return
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_run_once_computes_and_pushes_correlations(event_bus):
    exchange = FakeCorrelationExchange()
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    updater = SymbolCorrelationUpdater(
        exchange=exchange,
        graph_writer=writer,
        symbols=["BTCUSDT", "ETHUSDT", "XRPUSDT"],
        interval_seconds=1000,
    )
    await updater.initialize({})

    updated = await updater.run_once()

    assert updated == 3  # 3 choose 2 pairs
    correlation_calls = [
        c for c in driver.calls if c[0].strip().startswith("MERGE (a:Symbol")
    ]
    assert len(correlation_calls) == 3

    pairs = {
        (c[1]["symbol_a"], c[1]["symbol_b"]): c[1]["coefficient"]
        for c in correlation_calls
    }
    btc_eth = pairs.get(("BTCUSDT", "ETHUSDT"))
    btc_xrp = pairs.get(("BTCUSDT", "XRPUSDT"))
    assert btc_eth == pytest.approx(1.0, abs=1e-6)
    assert btc_xrp == pytest.approx(-1.0, abs=1e-6)

    health = await updater.health_check()
    assert health.details["pairs_updated_last_run"] == 3

    await updater.shutdown()


@pytest.mark.asyncio
async def test_run_once_isolates_per_symbol_fetch_failures(event_bus):
    class PartiallyFailingExchange(FakeCorrelationExchange):
        async def fetch_klines(self, symbol, timeframe, limit=500):
            if symbol == "BADSYMBOL":
                raise ConnectionError("no data")
            return await super().fetch_klines(symbol, timeframe, limit)

    exchange = PartiallyFailingExchange()
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    updater = SymbolCorrelationUpdater(
        exchange=exchange,
        graph_writer=writer,
        symbols=["BTCUSDT", "ETHUSDT", "BADSYMBOL"],
        interval_seconds=1000,
    )
    await updater.initialize({})

    updated = await updater.run_once()

    assert updated == 1  # only the BTCUSDT/ETHUSDT pair succeeds
    health = await updater.health_check()
    assert health.details["errors"] >= 1

    await updater.shutdown()


@pytest.mark.asyncio
async def test_background_loop_runs_automatically(event_bus):
    exchange = FakeCorrelationExchange()
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    updater = SymbolCorrelationUpdater(
        exchange=exchange,
        graph_writer=writer,
        symbols=["BTCUSDT", "ETHUSDT"],
        interval_seconds=0.05,
    )
    await updater.initialize({})

    for _ in range(40):
        if updater._pairs_updated_last_run > 0:
            break
        await asyncio.sleep(0.05)

    assert updater._pairs_updated_last_run == 1
    await updater.shutdown()
