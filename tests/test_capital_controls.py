from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.capital_controls import (
    CapitalCircuitBreaker,
    CapitalControlConfig,
    ProbabilityCalibrator,
    execution_cost_bps,
    opportunity_age_seconds,
)


def test_daily_loss_and_loss_streak_are_hard_stops():
    breaker = CapitalCircuitBreaker()
    assert breaker.check(daily_pnl_pct=-3.0)[0] is False
    assert breaker.check(consecutive_losses=5)[0] is False
    assert breaker.check(daily_pnl_pct=-2.9, consecutive_losses=4)[0] is True


def test_opportunity_expiry():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=31)).isoformat()
    assert opportunity_age_seconds(old, now=now) == pytest.approx(31.0)
    assert opportunity_age_seconds("not-a-timestamp") == float("inf")


def test_execution_cost_rises_with_bad_liquidity_and_volatility():
    normal = execution_cost_bps(
        base_fee_bps=10, base_slippage_bps=5, liquidity_score=8, volatility_score=0.1
    )
    stressed = execution_cost_bps(
        base_fee_bps=10, base_slippage_bps=5, liquidity_score=2, volatility_score=0.9
    )
    assert stressed > normal


def test_probability_calibrator_waits_for_minimum_samples_then_uses_empirical_rate():
    config = CapitalControlConfig(calibration_min_samples=4)
    calibrator = ProbabilityCalibrator(config)
    assert calibrator.calibrate(0.2) == pytest.approx(0.2)
    for _ in range(3):
        calibrator.observe(0.2, False)
    calibrator.observe(0.2, True)
    assert calibrator.samples == 4
    assert calibrator.calibrate(0.2) == pytest.approx(0.25)
