from types import SimpleNamespace

from aitos.intelligence.position_monitor import (
    PositionMonitorController,
    PositionMonitorTier,
)
from aitos.intelligence.position_runtime import (
    MAX_DEEP_SYMBOLS,
    _capital_policy_consensus,
    _merge_symbols,
)


def test_position_policy_keeps_monitoring_metadata_without_fixed_position_size():
    assert MAX_DEEP_SYMBOLS >= 1
    portfolio = SimpleNamespace(
        equity_usd=10_000.0,
        positions=(SimpleNamespace(notional_usd=1_000.0, symbol="BTCUSDT"),),
    )
    policy = _capital_policy_consensus(portfolio)
    assert policy["monitoring_model"] == "normal_warning_exit_candidate"
    assert (
        policy["position_sizing_authority"]
        == "existing_position_manager_and_risk_engine"
    )
    assert policy["fixed_per_position_capital_pct"] is None
    assert policy["fixed_per_position_capital_usd"] is None


def test_merge_symbols_preserves_requested_order_and_adds_open_positions():
    merged = _merge_symbols(
        ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        ["SOLUSDT", "ETHUSDT", "XRPUSDT"],
    )
    assert merged == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def _trade(trade_id: str = "t1", sl: float = 95.0):
    return SimpleNamespace(
        trade_id=trade_id,
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
    assert decision.health is not None
    assert decision.health.pnl_pct > 0
    assert decision.priority_score == 0.0


def test_position_monitor_warning_uses_hysteresis_before_returning_to_normal():
    controller = PositionMonitorController(hysteresis_updates=3)
    warning = controller.evaluate(
        trade=_trade(), current_price=95.5, extra_features={}
    )
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


def test_priority_score_prefers_more_urgent_warning_position():
    controller = PositionMonitorController()
    mild = controller.evaluate(
        trade=_trade("mild"),
        current_price=101.0,
        extra_features={"liquidity_risk": 0.2},
    )
    urgent = controller.evaluate(
        trade=_trade("urgent", sl=99.5),
        current_price=99.6,
        extra_features={
            "liquidity_risk": 0.9,
            "adverse_order_flow_risk": 0.9,
            "thesis_risk": 0.8,
            "volatility_risk": 0.8,
            "reference_risk": 0.8,
            "data_freshness_seconds": 6.0,
        },
    )
    assert urgent.priority_score > mild.priority_score


def test_priority_score_caps_at_100_and_health_vector_is_optional_input_only():
    decision = PositionMonitorController().evaluate(
        trade=_trade("stress", sl=100.0),
        current_price=100.0,
        extra_features={
            "exit_signal": True,
            "liquidity_risk": 1.0,
            "adverse_order_flow_risk": 1.0,
            "thesis_risk": 1.0,
            "volatility_risk": 1.0,
            "reference_risk": 1.0,
            "regime_risk": 1.0,
            "data_freshness_seconds": 100.0,
        },
    )
    assert decision.tier == PositionMonitorTier.EXIT_CANDIDATE
    assert 0.0 <= decision.priority_score <= 100.0
    assert decision.health is not None
