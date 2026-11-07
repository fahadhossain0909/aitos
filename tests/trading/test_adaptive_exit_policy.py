import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.adaptive_exit_policy import AdaptiveExitPolicy


def _trade(side=TradeSide.LONG):
    return Trade(
        trade_id="adaptive-test",
        symbol="BTCUSDT",
        side=side,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=2.0,
        strategy_id="test",
        agent_consensus={},
        explanation="test",
        sl_price=98.0 if side == TradeSide.LONG else 102.0,
        tp_price=104.0 if side == TradeSide.LONG else 96.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-01-01T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("side", "price", "expected"),
    [
        (TradeSide.LONG, 101.6, 100.3),
        (TradeSide.SHORT, 98.4, 99.7),
    ],
)
def test_break_even_ratchet_uses_immutable_initial_r(side, price, expected):
    policy = AdaptiveExitPolicy()
    trade = _trade(side)
    result = policy.evaluate(trade, price)
    assert result.stop_price == pytest.approx(expected)
    assert result.reason == "breakeven_ratchet"


def test_stop_is_not_loosened_after_price_reverses():
    policy = AdaptiveExitPolicy()
    trade = _trade()
    first = policy.evaluate(trade, 101.6)
    trade.sl_price = first.stop_price
    second = policy.evaluate(trade, 100.5)
    assert second.stop_price is None


def test_stagnation_requires_configured_number_of_observations():
    policy = AdaptiveExitPolicy({"stagnation_bars": 3})
    trade = _trade()
    # Repeated ticks within one completed candle count only once.
    assert not policy.evaluate(trade, 100.1, completed_bar_id="bar-1").stagnation_exit
    assert not policy.evaluate(trade, 100.15, completed_bar_id="bar-1").stagnation_exit
    assert not policy.evaluate(trade, 100.1, completed_bar_id="bar-2").stagnation_exit
    result = policy.evaluate(trade, 100.1, completed_bar_id="bar-3")
    assert result.stagnation_exit
    assert result.reason == "time_decay_scratch"
    assert result.bars_observed == 3


def test_dead_candle_exit_requires_sixteen_unique_completed_bars_near_entry():
    policy = AdaptiveExitPolicy(
        {
            "stagnation_bars": 16,
            "stagnation_min_progress_r": 0.10,
            "stagnation_entry_band_pct": 0.002,
        }
    )
    trade = _trade()
    for index in range(1, 16):
        result = policy.evaluate(trade, 100.1, completed_bar_id=f"900:{index * 900}")
        assert not result.stagnation_exit
    result = policy.evaluate(trade, 100.1, completed_bar_id="900:14400")
    assert result.stagnation_exit
    assert result.bars_observed == 16


def test_dead_candle_exit_does_not_fire_after_price_leaves_entry_band():
    policy = AdaptiveExitPolicy({"stagnation_bars": 2})
    trade = _trade()
    policy.evaluate(trade, 100.3, completed_bar_id="bar-1")
    result = policy.evaluate(trade, 100.1, completed_bar_id="bar-2")
    assert not result.stagnation_exit
    assert trade.mfe_r > 0.10


def test_dead_candle_exit_does_not_fire_after_meaningful_progress():
    policy = AdaptiveExitPolicy(
        {"stagnation_bars": 2, "stagnation_entry_band_pct": 0.005}
    )
    trade = _trade()
    trade.initial_r_distance = 1.0
    policy.evaluate(trade, 100.45, completed_bar_id="bar-1")
    result = policy.evaluate(trade, 100.1, completed_bar_id="bar-2")
    assert trade.mfe_r >= 0.4
    assert not result.stagnation_exit


def test_observation_counter_is_stored_on_trade_for_serialization():
    policy = AdaptiveExitPolicy({"stagnation_bars": 10})
    trade = _trade()
    policy.evaluate(trade, 100.1, completed_bar_id="bar-1")
    assert trade.adaptive_exit_observations == 1
    assert trade.to_dict()["adaptive_exit_observations"] == 1
    assert trade.to_dict()["adaptive_exit_last_bar_id"] == "bar-1"
    policy.evaluate(trade, 100.2, completed_bar_id="bar-1")
    assert trade.adaptive_exit_observations == 1


def test_invalid_risk_and_market_price_fail_safe():
    policy = AdaptiveExitPolicy()
    trade = _trade()
    trade.initial_r_distance = 0.0
    assert policy.evaluate(trade, 101.0).reason == "invalid_initial_risk"
    trade.initial_r_distance = 2.0
    assert policy.evaluate(trade, float("nan")).reason == "invalid_market_price"


def test_stagnation_entry_band_configuration_is_validated():
    with pytest.raises(ValueError, match="entry-price band"):
        AdaptiveExitPolicy({"stagnation_entry_band_pct": 0.1})
