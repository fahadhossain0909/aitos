import pytest

from aitos.risk.models import RiskLimits
from aitos.risk.position_sizing import (calculate_adaptive_leverage,
                                        calculate_position_size,
                                        kelly_fraction)


def test_kelly_fraction_positive_edge():
    f = kelly_fraction(win_rate=0.6, win_loss_ratio=2.0)
    assert f == pytest.approx(0.6 - 0.4 / 2.0)


def test_kelly_fraction_negative_edge_clamped_to_zero():
    f = kelly_fraction(win_rate=0.3, win_loss_ratio=1.0)
    assert f == 0.0


def test_kelly_fraction_invalid_inputs_raise():
    with pytest.raises(ValueError):
        kelly_fraction(win_rate=1.5, win_loss_ratio=1.0)
    with pytest.raises(ValueError):
        kelly_fraction(win_rate=0.5, win_loss_ratio=0.0)


def test_adaptive_leverage_decreases_with_volatility_and_risk_score():
    limits = RiskLimits(max_leverage=20.0)
    low = calculate_adaptive_leverage(
        volatility_percentile=10, risk_score=10, risk_limits=limits, base_leverage=10.0
    )
    high = calculate_adaptive_leverage(
        volatility_percentile=90, risk_score=90, risk_limits=limits, base_leverage=10.0
    )
    assert low > high
    assert 1.0 <= high <= limits.max_leverage
    assert 1.0 <= low <= limits.max_leverage


def test_adaptive_leverage_never_exceeds_configured_max():
    limits = RiskLimits(max_leverage=5.0)
    leverage = calculate_adaptive_leverage(
        volatility_percentile=0, risk_score=0, risk_limits=limits, base_leverage=50.0
    )
    assert leverage <= 5.0


def test_adaptive_leverage_floor_is_one():
    limits = RiskLimits(max_leverage=20.0)
    leverage = calculate_adaptive_leverage(
        volatility_percentile=100,
        risk_score=100,
        risk_limits=limits,
        base_leverage=10.0,
    )
    assert leverage >= 1.0


def test_calculate_position_size_basic():
    limits = RiskLimits()
    result = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=98.0,
        risk_limits=limits,
        risk_score=20.0,
        volatility_percentile=30.0,
    )
    # requested risk defaults to 1% of equity = $100, dampened by vol/corr factors
    assert 0 < result.risk_amount_usd <= 100.0
    assert result.position_size_usd > 0
    assert result.leverage >= 1.0
    assert "risk=" in result.rationale


def test_calculate_position_size_respects_hard_cap_even_if_requested_higher():
    limits = RiskLimits()
    result = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=98.0,
        risk_limits=limits,
        requested_risk_pct=50.0,  # way beyond hard cap
    )
    max_possible_risk_usd = 10_000.0 * (limits.max_risk_per_trade_hard_cap_pct / 100.0)
    assert result.risk_amount_usd <= max_possible_risk_usd


def test_calculate_position_size_kelly_reduces_size_for_weak_edge():
    limits = RiskLimits()
    strong_edge = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=98.0,
        risk_limits=limits,
        win_rate=0.65,
        win_loss_ratio=2.5,
    )
    weak_edge = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=98.0,
        risk_limits=limits,
        win_rate=0.40,
        win_loss_ratio=1.0,
    )
    assert weak_edge.risk_amount_usd < strong_edge.risk_amount_usd


def test_calculate_position_size_zero_stop_distance_raises():
    limits = RiskLimits()
    with pytest.raises(ValueError):
        calculate_position_size(
            equity_usd=10_000.0,
            entry_price=100.0,
            stop_loss_price=100.0,
            risk_limits=limits,
        )


def test_calculate_position_size_negative_equity_raises():
    limits = RiskLimits()
    with pytest.raises(ValueError):
        calculate_position_size(
            equity_usd=-1.0, entry_price=100.0, stop_loss_price=98.0, risk_limits=limits
        )
