import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.risk.models import RiskLimits
from aitos.risk.position_sizing import calculate_position_size


def _trade(*, side=TradeSide.LONG, entry=100.0, sl=98.0):
    return Trade(
        trade_id="trade-test",
        symbol="BTCUSDT",
        side=side,
        entry_price=entry,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=2.0,
        strategy_id="test",
        agent_consensus={},
        explanation="test",
        sl_price=sl,
        tp_price=104.0 if side == TradeSide.LONG else 96.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-31T00:00:00+00:00",
    )


def test_trade_r_distance_is_immutable_after_sl_moves():
    trade = _trade()
    assert trade.r_distance == pytest.approx(2.0)

    trade.sl_price = 100.0  # simulated break-even move
    assert trade.r_distance == pytest.approx(2.0)
    assert trade.unrealized_r_multiple(102.0) == pytest.approx(1.0)


def test_mae_mfe_use_initial_r_after_stop_moves():
    trade = _trade()
    trade.sl_price = 101.0  # simulated trailing stop
    trade.record_excursion(104.0)
    trade.record_excursion(96.0)

    assert trade.mae_price == pytest.approx(4.0)
    assert trade.mfe_price == pytest.approx(4.0)
    assert trade.mae_r == pytest.approx(2.0)
    assert trade.mfe_r == pytest.approx(2.0)


def test_position_sizing_risk_matches_final_notional_after_leverage_cap():
    limits = RiskLimits(max_leverage=1.0)
    result = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=99.0,
        risk_limits=limits,
        requested_risk_pct=5.0,
        volatility_percentile=0.0,
        correlation_penalty=0.0,
        base_leverage=10.0,
    )

    assert result.position_size_usd == pytest.approx(10_000.0)
    assert result.risk_amount_usd == pytest.approx(100.0)


def test_position_sizing_risk_matches_final_notional_after_sector_cap():
    limits = RiskLimits(max_leverage=20.0)
    result = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=90.0,
        risk_limits=limits,
        requested_risk_pct=5.0,
        volatility_percentile=0.0,
        correlation_penalty=0.0,
        existing_sector_notional_usd=2_000.0,
        sector_limit_pct=20.0,
    )

    assert result.position_size_usd == pytest.approx(0.0)
    assert result.risk_amount_usd == pytest.approx(0.0)
