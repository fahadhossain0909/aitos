"""Unit tests for Position Manager (Phase E)."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.intelligence.exit_intelligence import ExitAction
from aitos.intelligence.path_planner import TargetExtensionDecision
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.persistent_state import _trade_from_dict
from aitos.trading.position_manager import PositionManager


def _open_trade(
    side: TradeSide = TradeSide.LONG,
    entry: float = 79000.0,
    sl: float = 78500.0,
) -> Trade:
    return Trade(
        trade_id="trade-test001",
        symbol="BTCUSDT",
        side=side,
        entry_price=entry,
        quantity=0.1,
        leverage=5.0,
        position_size_usd=7900.0,
        risk_amount_usd=50.0,
        strategy_id="test",
        agent_consensus={},
        explanation="test",
        sl_price=sl,
        tp_price=80000.0,
        take_profit_levels=[80000.0],
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time=datetime.now(timezone.utc).isoformat(),
    )


def test_position_manager_returns_action():
    pm = PositionManager()
    trade = _open_trade()
    action = pm.evaluate(trade=trade, current_price=79400.0, trend_strength=0.8)
    assert action.action in (ExitAction.HOLD, ExitAction.MANAGE, ExitAction.EXIT)
    assert action.exit_decision is not None
    assert action.market_state is not None
    assert action.path_plan is not None
    assert action.structural_stop is not None
    assert action.reason.startswith("EIE:")


def test_hold_path_when_thesis_intact():
    pm = PositionManager()
    trade = _open_trade()
    action = pm.evaluate(
        trade=trade,
        current_price=79400.0,
        trend_strength=0.85,
        swing_lows=[78600.0],
        prior_highs=[80000.0],
    )
    # With bullish defaults and strong trend we expect HOLD or mild MANAGE
    assert action.action in (ExitAction.HOLD, ExitAction.MANAGE)


def test_to_dict_serializable():
    pm = PositionManager()
    action = pm.evaluate(trade=_open_trade(), current_price=79100.0)
    d = action.to_dict()
    assert "action" in d
    assert "exit_decision" in d


def test_target_extension_decision_is_returned_for_lifecycle_guard():
    pm = PositionManager(config={"hedge_enabled": False})
    trade = _open_trade()
    trade.take_profit_levels = [80000.0, 81000.0]
    expected = TargetExtensionDecision(
        approved=False,
        target_price=80500.0,
        proposed_price=81000.0,
        base_price=80500.0,
        reason="test veto",
        vetoes=("test_veto",),
    )
    pm._mpp.assess_target_extension = lambda **kwargs: expected

    action = pm.evaluate(
        trade=trade,
        current_price=79400.0,
        trend_strength=0.85,
        prior_highs=[81200.0],
    )

    assert action.target_extension is expected
    assert action.to_dict()["target_extension"]["vetoes"] == ["test_veto"]


def test_adaptive_exit_is_enabled_by_default_and_can_be_disabled():
    assert PositionManager()._adaptive_exit_enabled is True
    assert (
        PositionManager(config={"adaptive_exit_enabled": False})._adaptive_exit_enabled
        is False
    )


def test_adaptive_stagnation_bar_identity_uses_only_completed_buckets():
    from datetime import datetime, timezone

    manager = PositionManager(config={"adaptive_exit_bar_seconds": 900})
    first_tick = datetime.fromtimestamp(1800, tz=timezone.utc)
    repeated_tick = datetime.fromtimestamp(1850, tz=timezone.utc)
    next_bucket = datetime.fromtimestamp(2700, tz=timezone.utc)

    assert manager._completed_bar_identity(first_tick) == "900:900"
    assert manager._completed_bar_identity(repeated_tick) == "900:900"
    assert manager._completed_bar_identity(next_bucket) == "900:1800"


def test_adaptive_completed_bar_identity_survives_trade_recovery():
    trade = _open_trade()
    trade.adaptive_exit_observations = 12
    trade.adaptive_exit_last_bar_id = "900:1710000000"
    trade.last_marked_at = "2026-11-07T03:00:00+00:00"

    restored = _trade_from_dict(trade.to_dict())

    assert restored.adaptive_exit_observations == 12
    assert restored.adaptive_exit_last_bar_id == "900:1710000000"
    assert restored.last_marked_at == "2026-11-07T03:00:00+00:00"


def test_adaptive_stagnation_does_not_count_bars_closed_before_entry():
    manager = PositionManager(config={"adaptive_exit_bar_seconds": 900})
    trade = _open_trade()
    trade.entry_time = datetime.fromtimestamp(1850, tz=timezone.utc).isoformat()

    # At 31:00 the 15m candle ending at 30:00 closed before entry.
    assert (
        manager._completed_bar_identity(
            datetime.fromtimestamp(1860, tz=timezone.utc), trade
        )
        is None
    )

    # At 45:00 the 15m candle ending at 45:00 closed after entry.
    assert (
        manager._completed_bar_identity(
            datetime.fromtimestamp(2701, tz=timezone.utc), trade
        )
        == "900:1800"
    )
