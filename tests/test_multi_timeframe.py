from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.intelligence import multi_timeframe
from aitos.intelligence.multi_timeframe import (
    MultiTimeframeAnalyzer,
    REQUIRED_TIMEFRAMES,
)
from aitos.intelligence.htf_cache import timeframe_delta
from aitos.models.trade import TradeSide


class FakeCache:
    def __init__(self, stale=False):
        self.stale = stale
        self.calls = []

    async def get(self, symbol, timeframe, limit, fetch):
        self.calls.append((symbol, timeframe, limit))
        age = timeframe_delta(timeframe) * (3 if self.stale else 0.5)
        candle = SimpleNamespace(close_time=datetime.now(timezone.utc) - age)
        return [candle] * 40, {"cache": "refreshed", "stale": False}


class FakeExchange:
    async def fetch_klines(self, symbol, timeframe, limit):
        return []


@pytest.fixture
def deterministic_indicators(monkeypatch):
    monkeypatch.setattr(multi_timeframe.indicators, "adx", lambda candles: 30.0)
    monkeypatch.setattr(
        multi_timeframe.indicators, "di_plus_minus", lambda candles: (35.0, 10.0)
    )
    monkeypatch.setattr(
        multi_timeframe.indicators,
        "analyze_market_structure",
        lambda candles: SimpleNamespace(bias="bullish"),
    )
    monkeypatch.setattr(
        multi_timeframe.indicators, "classify_regime", lambda candles: "trending"
    )
    monkeypatch.setattr(
        multi_timeframe.indicators, "momentum_score", lambda candles: 8.0
    )


@pytest.mark.asyncio
async def test_analyzer_fetches_and_integrates_all_six_timeframes(
    deterministic_indicators,
):
    cache = FakeCache()
    result = await MultiTimeframeAnalyzer(cache).analyze(
        FakeExchange(),
        "BTCUSDT",
        TradeSide.LONG,
        [SimpleNamespace()] * 40,
        flow_score=8.0,
        structure_bias="bullish",
    )
    assert tuple(item.timeframe for item in result.snapshots) == REQUIRED_TIMEFRAMES
    assert len(cache.calls) == 6
    assert result.accepted
    assert result.decision.mode == "aligned"


@pytest.mark.asyncio
async def test_all_stale_timeframes_fail_closed(deterministic_indicators):
    result = await MultiTimeframeAnalyzer(FakeCache(stale=True)).analyze(
        FakeExchange(),
        "BTCUSDT",
        TradeSide.LONG,
        [SimpleNamespace()] * 40,
        flow_score=8.0,
        structure_bias="bullish",
    )
    assert not result.accepted
    assert set(result.diagnostics["stale_timeframes"]) == set(REQUIRED_TIMEFRAMES)
