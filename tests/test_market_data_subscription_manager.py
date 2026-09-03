from aitos.market_data.subscription_manager import SubscriptionManager
from aitos.market_data.stream_policy import build_subscription_plan


def test_plan_keeps_btc_and_promotes_two_non_btc() -> None:
    plan = build_subscription_plan(["ETHUSDT", "BTCUSDT", "SOLUSDT", "BNBUSDT"])
    assert plan.deep == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert plan.historical_book == ("BTCUSDT", "LTCUSDT")


def test_manager_emits_only_state_changes() -> None:
    manager = SubscriptionManager()
    first = manager.apply_ranked_symbols(["ETHUSDT", "SOLUSDT"])
    assert first.subscribe == ("ETHUSDT", "SOLUSDT")
    assert first.unsubscribe == ()

    second = manager.apply_ranked_symbols(["BNBUSDT", "SOLUSDT"])
    assert second.subscribe == ("BNBUSDT",)
    assert second.unsubscribe == ("ETHUSDT",)

    third = manager.apply_ranked_symbols(["BNBUSDT", "SOLUSDT"])
    assert third.subscribe == ()
    assert third.unsubscribe == ()


def test_reset_preserves_permanent_symbol() -> None:
    manager = SubscriptionManager()
    manager.apply_ranked_symbols(["ETHUSDT", "SOLUSDT"])
    delta = manager.reset()
    assert delta.unsubscribe == ("ETHUSDT", "SOLUSDT")
    assert manager.active == frozenset({"BTCUSDT"})
