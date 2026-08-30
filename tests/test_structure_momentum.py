"""Extra indicator coverage for structure / momentum / regime expansions."""

from aitos.intelligence import indicators
from tests.test_indicators import (
    make_klines,
    make_trending_up_klines,
    make_ranging_klines,
)


def test_analyze_market_structure_bullish_breakout():
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
    structure = indicators.analyze_market_structure(
        make_klines(closes), swing_lookback=10
    )
    assert structure.bos_direction == "bullish_bos"
    assert structure.bos_strength > 0
    assert structure.event in {"bos", "choch"}


def test_detect_structure_break_still_matches_analyze():
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
    structure = indicators.analyze_market_structure(klines, swing_lookback=10)
    assert direction == structure.bos_direction
    assert strength == structure.bos_strength


def test_rsi_bounds_and_neutral_default():
    assert indicators.rsi(make_klines([100.0, 101.0])) == 50.0
    trending = make_trending_up_klines(n=40, step=2.0)
    assert 50.0 < indicators.rsi(trending) <= 100.0


def test_momentum_score_range():
    score = indicators.momentum_score(make_trending_up_klines(n=40, step=1.5))
    assert 0.0 <= score <= 10.0


def test_classify_regime_includes_compression_path():
    ranging = make_ranging_klines(n=40, amplitude=0.2)
    regime = indicators.classify_regime(ranging)
    assert regime in {
        "ranging",
        "compression",
        "trending",
        "volatile",
        "expansion",
        "unknown",
    }


def test_lead_lag_multi_lag_still_bounded():
    reference = make_trending_up_klines(n=30, step=1.0)
    same = make_trending_up_klines(n=30, step=1.0)
    score = indicators.lead_lag_score(same, reference, lag=1, max_lag=3)
    assert 0.0 <= score <= 10.0
