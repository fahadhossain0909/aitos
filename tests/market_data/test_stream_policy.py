from aitos.market_data.stream_policy import (
    HISTORICAL_DEEP_SYMBOLS,
    build_subscription_plan,
)


def test_live_deep_tier_is_btc_plus_two_ranked_non_btc() -> None:
    plan = build_subscription_plan(
        ["ETHUSDT", "SOLUSDT", "BTCUSDT", "BNBUSDT", "XRPUSDT"]
    )
    assert plan.deep == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_historical_deep_tier_is_fixed_btc_ltc() -> None:
    plan = build_subscription_plan(["ETHUSDT", "SOLUSDT", "BTCUSDT", "BNBUSDT"])
    assert plan.historical_book == HISTORICAL_DEEP_SYMBOLS == ("BTCUSDT", "LTCUSDT")


def test_live_ranking_never_changes_historical_symbols() -> None:
    first = build_subscription_plan(["SOLUSDT", "ETHUSDT", "BTCUSDT"])
    second = build_subscription_plan(["DOGEUSDT", "BNBUSDT", "BTCUSDT"])
    assert first.historical_book == second.historical_book == ("BTCUSDT", "LTCUSDT")
