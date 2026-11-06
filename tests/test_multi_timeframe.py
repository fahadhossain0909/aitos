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
    monkeypatch.setattr(
        multi_timeframe,
        "_candle_evidence",
        lambda candles: {
            "structure": 8.0,
            "order_flow": 8.0,
            "momentum": 8.0,
            "liquidity": 7.0,
            "location": 8.0,
            "participation": 6.0,
            "relative_volume": 1.4,
            "lower_sweep_reclaim": 1.0,
            "upper_sweep_reclaim": 0.0,
        },
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


def test_candle_evidence_derives_liquidity_location_and_participation(monkeypatch):
    class Candle:
        def __init__(self, close, high, low, volume, open_price=None):
            self.close = close
            self.high = high
            self.low = low
            self.open = close if open_price is None else open_price
            self.volume = volume

    candles = [Candle(100.0, 101.0, 99.0, 100.0) for _ in range(40)]
    candles[-1] = Candle(100.8, 101.0, 98.0, 200.0, open_price=99.0)
    monkeypatch.setattr(
        multi_timeframe.indicators,
        "analyze_market_structure",
        lambda bars: SimpleNamespace(structure_score=8.0),
    )
    monkeypatch.setattr(multi_timeframe.indicators, "momentum_score", lambda bars: 7.0)
    monkeypatch.setattr(multi_timeframe.indicators, "cvd_trend_score", lambda bars: 6.0)
    result = multi_timeframe._candle_evidence(candles)
    assert result["structure"] == 8.0
    assert result["momentum"] == 7.0
    assert result["order_flow"] == 6.0
    assert result["participation"] > 5.0
    assert result["location"] > 5.0
    assert 0.0 <= result["liquidity"] <= 10.0


def test_participation_pressure_is_signed_and_double_sweep_is_not_bullish(
    monkeypatch,
):
    class Candle:
        def __init__(self, open_price, close, high, low, volume):
            self.open = open_price
            self.close = close
            self.high = high
            self.low = low
            self.volume = volume

    candles = [Candle(100.0, 100.0, 101.0, 99.0, 100.0) for _ in range(40)]
    candles[-1] = Candle(101.0, 99.2, 102.0, 98.0, 200.0)
    monkeypatch.setattr(
        multi_timeframe.indicators,
        "analyze_market_structure",
        lambda bars: SimpleNamespace(structure_score=4.0),
    )
    monkeypatch.setattr(multi_timeframe.indicators, "momentum_score", lambda bars: 4.0)
    monkeypatch.setattr(multi_timeframe.indicators, "cvd_trend_score", lambda bars: 4.0)
    result = multi_timeframe._candle_evidence(candles)
    assert result["participation"] < 5.0
    assert result["lower_sweep_reclaim"] == 1.0
    assert result["upper_sweep_reclaim"] == 1.0
    assert 4.5 <= result["liquidity"] <= 5.5



def test_sweep_reclaim_requires_directional_displacement_for_confirmation(
    monkeypatch,
):
    class Candle:
        def __init__(self, open_price, close, high, low, volume=100.0):
            self.open = open_price
            self.close = close
            self.high = high
            self.low = low
            self.volume = volume

    candles = [Candle(100.0, 100.0, 101.0, 99.0) for _ in range(40)]
    candles[-1] = Candle(97.5, 100.5, 101.0, 97.0, volume=200.0)
    monkeypatch.setattr(
        multi_timeframe.indicators,
        "analyze_market_structure",
        lambda bars: SimpleNamespace(structure_score=8.0),
    )
    monkeypatch.setattr(multi_timeframe.indicators, "momentum_score", lambda bars: 8.0)
    monkeypatch.setattr(
        multi_timeframe.indicators, "cvd_trend_score", lambda bars: 8.0
    )
    bullish = multi_timeframe._candle_evidence(candles)
    assert bullish["lower_sweep_reclaim"] == 1.0
    assert bullish["bullish_sweep_displacement"] == 1.0

    candles[-1] = Candle(102.5, 99.5, 103.0, 99.0, volume=200.0)
    bearish = multi_timeframe._candle_evidence(candles)
    assert bearish["upper_sweep_reclaim"] == 1.0
    assert bearish["bearish_sweep_displacement"] == 1.0
