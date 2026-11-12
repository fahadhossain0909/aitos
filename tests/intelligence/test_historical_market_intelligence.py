import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from aitos.intelligence.historical_market_intelligence import (
    HistoricalIntelligenceConfig,
    OnDemandHistoricalIntelligence,
    kline_request_weight,
)
from aitos.models.market import Kline


def k(i):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    p = 100 + i * 0.25 + (i % 5)
    return Kline(
        "BTCUSDT",
        "1m",
        t,
        t + timedelta(minutes=1),
        p,
        p + 1,
        p - 1,
        p,
        100,
        10000,
        20,
        50,
        5000,
    )


@pytest.mark.parametrize(
    "limit,weight",
    [(99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1001, 10), (1500, 10)],
)
def test_binance_kline_weight(limit, weight):
    assert kline_request_weight(limit) == weight


@pytest.mark.asyncio
async def test_same_candidate_request_is_coalesced_and_cached():
    calls = 0
    gate = asyncio.Event()

    async def fetcher(symbol, timeframe, limit):
        nonlocal calls
        calls += 1
        await gate.wait()
        return [k(i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(fetcher)
    a = asyncio.create_task(service.get_klines("BTCUSDT", "1m", limit=80))
    b = asyncio.create_task(service.get_klines("BTCUSDT", "1m", limit=80))
    await asyncio.sleep(0)
    gate.set()
    x, y = await asyncio.gather(a, b)
    assert calls == 1 and x == y
    assert service.metrics["coalesced_requests"] == 1
    assert await service.get_klines("BTCUSDT", "1m", limit=80) == x


@pytest.mark.asyncio
async def test_cache_is_bounded_by_entry_and_candle_count():
    async def fetcher(symbol, timeframe, limit):
        return [k(i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(
        fetcher,
        config=HistoricalIntelligenceConfig(max_cache_entries=2, max_cached_candles=90),
    )
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        await service.get_klines(s, "1m", limit=40)
    assert len(service._cache) <= 2 and service._cached_candles <= 90


@pytest.mark.asyncio
async def test_shared_redis_budget_fails_closed_if_unavailable():
    class BrokenRedis:
        async def get(self, *args):
            return None

        async def set(self, *args, **kwargs):
            return True

        async def eval(self, *args):
            raise RuntimeError("redis unavailable")

    calls = 0

    async def fetcher(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [k(i) for i in range(80)]

    service = OnDemandHistoricalIntelligence(fetcher, redis_client=BrokenRedis())
    assert await service.get_klines("BTCUSDT", "1m", limit=80) is None
    assert calls == 0


def test_context_is_evidence_only_and_excludes_current_window_from_matches():
    service = OnDemandHistoricalIntelligence(lambda *a, **k: None)
    rows = [k(i) for i in range(160)]
    context = service.build_context("BTCUSDT", "1m", rows)
    assert context["evidence_only"] and context["lookahead_policy"]
    current = len(rows) - service.config.analogue_window
    assert all(
        i + service.config.analogue_window + service.config.analogue_horizon - 1
        < current
        for i in context["analogue_indices"]
    )


@pytest.mark.asyncio
async def test_monthly_and_minute_timeframes_do_not_collide():
    requested = []

    async def fetcher(symbol, timeframe, limit):
        requested.append(timeframe)
        return [k(i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(fetcher)
    minute = await service.get_klines("BTCUSDT", "1m", limit=80)
    monthly = await service.get_klines("BTCUSDT", "1M", limit=80)

    assert minute and monthly
    assert requested == ["1m", "1M"]
    assert len(service._cache) == 2


def test_unknown_historical_timeframe_is_rejected():
    from aitos.intelligence.historical_market_intelligence import (
        _normalize_timeframe,
    )

    with pytest.raises(ValueError, match="unsupported historical timeframe"):
        _normalize_timeframe("2M")


@pytest.mark.asyncio
async def test_multi_timeframe_analysis_includes_primary_and_configured_intervals():
    requested = []

    async def fetcher(symbol, timeframe, limit):
        requested.append(timeframe)
        return [k(i) for i in range(limit)]

    service = OnDemandHistoricalIntelligence(fetcher)
    rows, evidence = await service.analyze_multi_timeframe("BTCUSDT", "1m", limit=80)

    assert rows and evidence["multi_timeframe"] is True
    assert evidence["timeframe"] == "1m"
    assert set(evidence["timeframe_evidence"]) == {"1m", "5m", "15m", "1h", "4h"}
    assert set(requested) == {"1m", "5m", "15m", "1h", "4h"}
    assert len(requested) == 5
