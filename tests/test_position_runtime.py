from types import SimpleNamespace

from aitos.intelligence.position_monitor import (
    PositionMonitorController,
    PositionMonitorTier,
)
from aitos.intelligence.position_runtime import (
    MAX_DEEP_SYMBOLS,
    MAX_OPEN_POSITIONS,
    POSITION_CAPITAL_POOL_PCT,
    POSITION_DATA_RESERVE_PCT,
    _capital_policy_consensus,
    _merge_symbols,
)


def test_position_policy_is_configurable_and_keeps_reserve_buffer():
    assert MAX_OPEN_POSITIONS == 10
    assert POSITION_DATA_RESERVE_PCT == 20.0
    assert POSITION_CAPITAL_POOL_PCT == 80.0
    assert MAX_DEEP_SYMBOLS == 6


def test_merge_symbols_preserves_requested_order_and_adds_open_positions():
    merged = _merge_symbols(
        ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        ["SOLUSDT", "ETHUSDT", "XRPUSDT"],
    )
    assert merged == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def test_capital_policy_reports_reserve_and_remaining_capacity():
    portfolio = SimpleNamespace(
        equity_usd=10_000.0,
        positions=(
            SimpleNamespace(notional_usd=1_000.0, symbol="BTCUSDT"),
            SimpleNamespace(notional_usd=500.0, symbol="ETHUSDT"),
        ),
    )
    policy = _capital_policy_consensus(portfolio)
    assert policy["capital_pool_usd"] == 8_000.0
    assert policy["deployed_notional_usd"] == 1_500.0
    assert policy["remaining_policy_capacity_usd"] == 6_500.0
    assert policy["monitoring_model"] == "normal_warning_exit_candidate"


def _trade(sl: float = 95.0):
    return SimpleNamespace(
        trade_id="t1",
        symbol="ETHUSDT",
        side=SimpleNamespace(value="LONG"),
        entry_price=100.0,
        sl_price=sl,
        record_excursion=lambda _price: None,
    )


def test_position_monitor_stays_normal_for_healthy_position():
    decision = PositionMonitorController().evaluate(
        trade=_trade(), current_price=102.0, extra_features={}
    )
    assert decision.tier == PositionMonitorTier.NORMAL


def test_position_monitor_warning_uses_hysteresis_before_returning_to_normal():
    controller = PositionMonitorController(hysteresis_updates=3)
    warning = controller.evaluate(trade=_trade(), current_price=95.5, extra_features={})
    assert warning.tier == PositionMonitorTier.WARNING
    first_clear = controller.evaluate(
        trade=_trade(), current_price=102.0, extra_features={}
    )
    assert first_clear.tier == PositionMonitorTier.WARNING
    second_clear = controller.evaluate(
        trade=_trade(), current_price=102.0, extra_features={}
    )
    assert second_clear.tier == PositionMonitorTier.WARNING
    final_clear = controller.evaluate(
        trade=_trade(), current_price=102.0, extra_features={}
    )
    assert final_clear.tier == PositionMonitorTier.NORMAL


def test_position_monitor_uses_adverse_order_flow_features():
    decision = PositionMonitorController().evaluate(
        trade=_trade(),
        current_price=101.0,
        extra_features={"cvd": -10.0, "delta": -5.0},
    )
    assert decision.tier == PositionMonitorTier.WARNING
    assert "cvd_divergence" in decision.reasons
