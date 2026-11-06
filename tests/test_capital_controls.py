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


def test_loss_streak_risk_throttle_config_is_bounded():
    with pytest.raises(ValueError):
        CapitalControlConfig(risk_reduction_multiplier=1.5)
    with pytest.raises(ValueError):
        CapitalControlConfig(risk_reduction_after_losses=0)


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



def test_probability_calibrator_persists_and_deduplicates_trade_outcomes(tmp_path):
    path = tmp_path / "calibration.sqlite3"
    config = CapitalControlConfig(calibration_min_samples=2)
    first = ProbabilityCalibrator(config, path=path)
    assert first.observe(0.2, True, observation_id="trade-1") is True
    assert first.observe(0.2, True, observation_id="trade-1") is False
    first.close()

    restarted = ProbabilityCalibrator(config, path=path)
    assert restarted.samples == 1
    assert restarted.calibrate(0.2) == pytest.approx(0.2)
    assert restarted.observe(0.2, False, observation_id="trade-1") is False
    assert restarted.observe(0.2, False, observation_id="trade-2") is True
    assert restarted.samples == 2
    assert restarted.calibrate(0.2) == pytest.approx(0.5)
    restarted.close()


def test_probability_calibrator_deduplicates_across_instances(tmp_path):
    path = tmp_path / "shared-calibration.sqlite3"
    config = CapitalControlConfig(calibration_min_samples=1)
    first = ProbabilityCalibrator(config, path=path)
    second = ProbabilityCalibrator(config, path=path)
    try:
        assert first.observe(0.7, True, observation_id="same-trade") is True
        assert second.observe(0.7, True, observation_id="same-trade") is False
        assert second.observe(0.7, False, observation_id="another-trade") is True
        assert first.samples == 2
        assert first.calibrate(0.7) == pytest.approx(0.5)
        # A fresh reader sees the committed, globally deduplicated total.
        first.close()
        second.close()
        reader = ProbabilityCalibrator(config, path=path)
        assert reader.samples == 2
        assert reader.calibrate(0.7) == pytest.approx(0.5)
        reader.close()
    finally:
        first.close()
        second.close()
