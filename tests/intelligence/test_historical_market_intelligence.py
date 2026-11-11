import asyncio
from types import SimpleNamespace

import pytest

from aitos.intelligence.historical_market_intelligence import (
    HistoricalIntelligenceConfig,
    OnDemandHistoricalIntelligence,
    kline_request_weight,
)


@pytest.mark.parametrize("limit,weight", [(99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1001, 10), (1500, 10)])
def test_binance_kline_request_weight(limit, weight):
    assert kline_request_weight(limit) == weight


@pytest.mark.asyncio
async def test_cache_and_inflight_requests_are_coalesced():
    calls = 0
    gate = asyncio.Event()

    async def fetcher(symbol, timeframe, limit):
        nonlocal calls
        calls += 1
        await gate.wait()
        return [SimpleNamespace(open_time=i, close=float(i + 1)) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(fetcher)
    first = asyncio.create_task(service.get_klines("BTCUSDT", "1m", limit=60))
    second = asyncio.create_task(service.get_klines("BTCUSDT", "1m", limit=60))
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert calls == 1
    assert a == b
    assert service.metrics["coalesced_requests"] == 1
    assert await service.get_klines("BTCUSDT", "1m", limit=60) == a
    assert service.metrics["cache_hits"] == 1


@pytest.mark.asyncio
async def test_weight_budget_rejects_without_fetching():
    calls = 0

    async def fetcher(symbol, timeframe, limit):
        nonlocal calls
        calls += 1
        return [SimpleNamespace(open_time=i, close=100 + i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(
        fetcher,
        HistoricalIntelligenceConfig(max_request_weight_per_minute=1),
    )
    assert await service.get_klines("BTCUSDT", "1m", limit=80)
    assert await service.get_klines("ETHUSDT", "1m", limit=80) is None
    assert calls == 1
    assert service.metrics["budget_rejections"] == 1


@pytest.mark.asyncio
async def test_cache_memory_is_bounded():
    async def fetcher(symbol, timeframe, limit):
        return [SimpleNamespace(open_time=i, close=100 + i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(
        fetcher,
        HistoricalIntelligenceConfig(max_cache_entries=2, max_cached_candles=100),
    )
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        await service.get_klines(symbol, "1m", limit=40)
    assert len(service._cache) <= 2
    assert service._cached_candles <= 100
