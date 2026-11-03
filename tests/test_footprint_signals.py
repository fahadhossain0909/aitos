from datetime import datetime, timezone

from aitos.intelligence.footprint import Footprint, FootprintLevel
from aitos.intelligence.footprint_signals import FootprintSignalEngine


def make_fp(levels):
    return Footprint(
        "BTCUSDT",
        datetime.now(timezone.utc),
        datetime.now(timezone.utc),
        1.0,
        tuple(levels),
    )


def test_empty_is_neutral():
    s = FootprintSignalEngine().evaluate(None)
    assert s.bias == "neutral"
    assert s.delta_score == 5.0


def test_bullish_footprint():
    fp = make_fp([FootprintLevel(100, 1, 9), FootprintLevel(101, 1, 7)])
    s = FootprintSignalEngine().evaluate(fp)
    assert s.bias == "bullish"
    assert s.delta_score > 5
    assert s.imbalance_score > 5


def test_bearish_footprint():
    fp = make_fp([FootprintLevel(100, 9, 1), FootprintLevel(101, 7, 1)])
    s = FootprintSignalEngine().evaluate(fp)
    assert s.bias == "bearish"
    assert s.delta_score < 5


def test_absorption_proxy_is_nonzero_for_high_volume_low_delta():
    fp = make_fp([FootprintLevel(100, 50, 52), FootprintLevel(101, 50, 48)])
    s = FootprintSignalEngine().evaluate(fp)
    assert s.absorption_score > 0


def test_scores_are_bounded():
    fp = make_fp([FootprintLevel(100, 0, 1000)])
    s = FootprintSignalEngine().evaluate(fp)
    assert 0 <= s.delta_score <= 10
    assert 0 <= s.imbalance_score <= 10
    assert 0 <= s.absorption_score <= 10
    assert 0 <= s.exhaustion_score <= 10
