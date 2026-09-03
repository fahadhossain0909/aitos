import pytest

from aitos.market_data.scanner_policy import (
    InstrumentScore,
    ScanLimits,
    ScanTier,
    deep_symbols,
    promote,
)


def test_promotion_is_deterministic() -> None:
    ranked = [
        InstrumentScore("SOLUSDT", 0.9),
        InstrumentScore("ETHUSDT", 0.9),
        InstrumentScore("BTCUSDT", 0.7),
        InstrumentScore("BNBUSDT", 0.5),
    ]
    tiers = promote(ranked, ScanLimits(top_25=3, top_10=2, top_5=2, top_2=2))
    assert tiers[ScanTier.TOP_2] == ["ETHUSDT", "SOLUSDT"]


def test_deep_scan_is_btc_plus_two_best_non_btc() -> None:
    ranked = [
        InstrumentScore("SOLUSDT", 1.0),
        InstrumentScore("BTCUSDT", 0.1),
        InstrumentScore("ETHUSDT", 0.9),
        InstrumentScore("BNBUSDT", 0.8),
    ]
    assert deep_symbols(ranked) == ["BTCUSDT", "SOLUSDT", "ETHUSDT"]


def test_scan_limits_reject_invalid_order() -> None:
    with pytest.raises(ValueError):
        ScanLimits(top_25=2, top_10=3, top_5=2, top_2=1)
