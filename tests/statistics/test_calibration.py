import pytest

from aitos.statistics.validation import (
    expected_calibration_error,
    purged_walk_forward_folds,
)


def test_expected_calibration_error_is_zero_for_perfectly_calibrated_bins():
    assert expected_calibration_error(
        [0.0, 0.0, 1.0, 1.0], [0, 0, 1, 1], n_bins=2
    ) == pytest.approx(0.0)


def test_expected_calibration_error_penalizes_overconfidence():
    value = expected_calibration_error([0.9, 0.9, 0.1, 0.1], [0, 0, 1, 1])
    assert value == pytest.approx(0.9)


def test_calibration_and_walk_forward_reject_invalid_inputs():
    with pytest.raises(ValueError):
        expected_calibration_error([0.5], [2])
    with pytest.raises(ValueError):
        expected_calibration_error([0.5], [1], n_bins=0)
    with pytest.raises(ValueError):
        purged_walk_forward_folds(100, test_size=0)
