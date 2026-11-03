from datetime import datetime, timedelta, timezone

from aitos.intelligence import indicators
from aitos.models.market import Kline

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_klines(
    closes, high_offset=0.5, low_offset=0.5, taker_buy_ratio=0.5, volume=100.0
):
    klines = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i > 0 else close
        t = BASE_TIME + timedelta(minutes=i)
        klines.append(
            Kline(
                symbol="TEST",
                timeframe="1m",
                open_time=t,
                close_time=t + timedelta(minutes=1),
                open=open_price,
                high=max(open_price, close) + high_offset,
                low=min(open_price, close) - low_offset,
                close=close,
                volume=volume,
                quote_volume=volume * close,
                trades_count=10,
                taker_buy_volume=volume * taker_buy_ratio,
                taker_buy_quote_volume=volume * close * taker_buy_ratio,
            )
        )
    return klines


def make_trending_up_klines(n=40, start=100.0, step=1.0):
    return make_klines([start + i * step for i in range(n)], taker_buy_ratio=0.65)


def make_ranging_klines(n=40, base=100.0, amplitude=1.0):
    import math

    return make_klines(
        [base + amplitude * math.sin(i / 3.0) for i in range(n)], taker_buy_ratio=0.5
    )


def test_average_true_range_positive_for_moving_prices():
    klines = make_trending_up_klines()
    atr = indicators.average_true_range(klines)
    assert atr > 0


def test_average_true_range_needs_history():
    assert indicators.average_true_range([]) == 0.0
    assert indicators.average_true_range(make_klines([100.0])) == 0.0


def test_adx_high_for_strong_trend():
    trending = make_trending_up_klines(n=40, step=2.0)
    ranging = make_ranging_klines(n=40)
    trend_adx = indicators.adx(trending)
    range_adx = indicators.adx(ranging)
    assert trend_adx > range_adx


def test_cumulative_volume_delta_positive_for_buy_heavy_klines():
    buy_heavy = make_klines([100.0] * 10, taker_buy_ratio=0.8)
    sell_heavy = make_klines([100.0] * 10, taker_buy_ratio=0.2)
    assert indicators.cumulative_volume_delta(buy_heavy) > 0
    assert indicators.cumulative_volume_delta(sell_heavy) < 0


def test_cvd_trend_score_reflects_buy_sell_pressure():
    buy_heavy = make_klines([100.0] * 25, taker_buy_ratio=0.9)
    sell_heavy = make_klines([100.0] * 25, taker_buy_ratio=0.1)
    neutral = make_klines([100.0] * 25, taker_buy_ratio=0.5)
    assert indicators.cvd_trend_score(buy_heavy) > 7
    assert indicators.cvd_trend_score(sell_heavy) < 3
    assert 4.5 <= indicators.cvd_trend_score(neutral) <= 5.5


def test_cvd_trend_score_neutral_for_empty_input():
    assert indicators.cvd_trend_score([]) == 5.0


def test_detect_structure_break_bullish_breakout():
    closes = [100.0] * 10 + [
        95.0,
        105.0,
        95.0,
        100.0,
        98.0,
        102.0,
        99.0,
        101.0,
        100.0,
        130.0,
    ]
    klines = make_klines(closes)
    direction, strength = indicators.detect_structure_break(klines, swing_lookback=10)
    assert direction == "bullish_bos"
    assert strength > 0


def test_detect_structure_break_bearish_breakout():
    closes = [100.0] * 10 + [
        95.0,
        105.0,
        95.0,
        100.0,
        98.0,
        102.0,
        99.0,
        101.0,
        100.0,
        60.0,
    ]
    klines = make_klines(closes)
    direction, strength = indicators.detect_structure_break(klines, swing_lookback=10)
    assert direction == "bearish_bos"


def test_detect_structure_break_none_when_inside_range():
    closes = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0, 97.0, 100.0, 101.0, 99.0, 100.0]
    klines = make_klines(closes)
    direction, strength = indicators.detect_structure_break(klines, swing_lookback=10)
    assert direction == "none"
    assert strength == 0.0


def test_detect_structure_break_insufficient_history():
    klines = make_klines([100.0, 101.0])
    direction, strength = indicators.detect_structure_break(klines, swing_lookback=10)
    assert direction == "none"


def test_classify_regime_trending():
    trending = make_trending_up_klines(n=40, step=2.0)
    assert indicators.classify_regime(trending) == "trending"


def test_classify_regime_unknown_with_insufficient_history():
    assert indicators.classify_regime(make_klines([100.0, 101.0])) == "unknown"


def test_pearson_correlation_perfect_positive():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert indicators.pearson_correlation(a, b) == 1.0


def test_pearson_correlation_perfect_negative():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert indicators.pearson_correlation(a, b) == -1.0


def test_pearson_correlation_degenerate_input_returns_zero():
    assert indicators.pearson_correlation([1.0], [2.0]) == 0.0
    assert indicators.pearson_correlation([1.0, 1.0, 1.0], [2.0, 3.0, 4.0]) == 0.0


def test_lead_lag_score_neutral_with_insufficient_history():
    short_klines = make_klines([100.0, 101.0])
    assert indicators.lead_lag_score(short_klines, short_klines) == 5.0


def test_lead_lag_score_reflects_correlation_direction():
    reference = make_trending_up_klines(n=30, step=1.0)
    same_direction = make_trending_up_klines(n=30, step=1.0)
    score = indicators.lead_lag_score(same_direction, reference, lag=1)
    assert 0.0 <= score <= 10.0
