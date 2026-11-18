from datetime import datetime, timedelta, timezone
from math import log

import pytest

from aitos.world_intelligence.calibration import (
    CalibrationObservation,
    calibration_cohorts,
    calibration_report,
)

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def observation(
    index,
    probability,
    outcome,
    *,
    asset="BTCUSDT",
    horizon="1h",
    regime="trend",
    mature=True,
):
    predicted = BASE + timedelta(hours=index)
    return CalibrationObservation(
        observation_id=str(index),
        asset=asset,
        horizon=horizon,
        regime=regime,
        predicted_at=predicted,
        outcome_available_at=predicted + timedelta(hours=1 if mature else 100),
        probability_up=probability,
        outcome_up=outcome,
    )


def test_report_excludes_outcomes_not_yet_available():
    rows = [observation(0, 0.8, True), observation(1, 0.99, False, mature=False)]
    report = calibration_report(rows, as_of=BASE + timedelta(hours=3))
    assert report.sample_count == 1
    assert report.excluded_unmatured == 1
    assert report.brier_score == pytest.approx(0.04)


def test_probability_one_is_in_last_bin_and_log_loss_is_finite():
    report = calibration_report(
        [observation(0, 1.0, True)], as_of=BASE + timedelta(hours=2)
    )
    assert report.bins[-1].sample_count == 1
    assert report.log_loss == pytest.approx(-log(1.0 - 1e-12))


def test_empty_report_has_explicit_empty_bins():
    report = calibration_report([], as_of=BASE, bin_count=5)
    assert report.sample_count == 0
    assert report.expected_calibration_error is None
    assert len(report.bins) == 5
    assert all(item.sample_count == 0 for item in report.bins)


def test_duplicate_observation_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        calibration_report(
            [observation(0, 0.2, False), observation(0, 0.8, True)],
            as_of=BASE + timedelta(days=1),
        )


def test_calibration_is_reported_separately_by_asset_horizon_and_regime():
    rows = [
        observation(0, 0.8, True),
        observation(1, 0.2, False, asset="ETHUSDT", horizon="4h", regime="range"),
    ]
    reports = calibration_cohorts(rows, as_of=BASE + timedelta(days=1))
    assert set(reports) == {
        ("BTCUSDT", "1h", "trend"),
        ("ETHUSDT", "4h", "range"),
    }
    assert all(report.sample_count == 1 for report in reports.values())


@pytest.mark.parametrize("bin_count", [0, 1, 51])
def test_invalid_bin_count_is_rejected(bin_count):
    with pytest.raises(ValueError, match="bin_count"):
        calibration_report([], as_of=BASE, bin_count=bin_count)


def test_observation_rejects_naive_timestamps_and_invalid_probability():
    with pytest.raises(ValueError, match="timezone-aware"):
        CalibrationObservation(
            observation_id="x",
            asset="BTC",
            horizon="1h",
            regime="trend",
            predicted_at=datetime(2026, 1, 1),
            outcome_available_at=BASE,
            probability_up=0.5,
            outcome_up=True,
        )
    with pytest.raises(ValueError, match="probability_up"):
        observation(0, 1.2, True)
