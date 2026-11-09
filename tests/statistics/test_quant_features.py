import math

import pytest

from aitos.statistics.quant_features import (
    cusum_change_point,
    distribution_features,
    exponential_hawkes_intensity,
    har_rv_forecast,
    hurst_exponent,
    kalman_beta,
    permutation_entropy,
)
from aitos.statistics.risk_overlay import assess_statistical_risk
from aitos.statistics.validation import (
    brier_score,
    log_loss,
    purged_walk_forward_folds,
    qlike_loss,
)


def test_distribution_and_volatility_features_are_finite():
    xs = [0.001, -0.002, 0.003, -0.001] * 20
    result = distribution_features(xs)
    forecast = har_rv_forecast(xs)
    assert result.count == len(xs)
    assert result.downside_semivolatility > 0
    assert forecast.forecast > 0
    assert 0 <= forecast.jump_ratio <= 1


def test_entropy_and_hurst_are_bounded():
    xs = [math.sin(i / 5) * 0.01 for i in range(100)]
    assert 0 <= permutation_entropy(xs) <= 1
    assert 0 <= hurst_exponent(xs) <= 1


def test_cusum_detects_large_persistent_shift():
    xs = [0.0] * 50 + [0.05] * 30
    result = cusum_change_point(xs, threshold=5, drift=0.1)
    assert result.score > 0


def test_event_intensity_and_kalman_beta():
    hawkes = exponential_hawkes_intensity([1, 1, 1, -1, 0])
    assert hawkes.buy > hawkes.sell
    beta = kalman_beta([2 * x for x in range(1, 20)], list(range(1, 20)))
    assert beta == pytest.approx(2.0, rel=0.1)


def test_forecast_validation_metrics():
    assert brier_score([0.9, 0.1], [1, 0]) == pytest.approx(0.01)
    assert log_loss([0.9, 0.1], [1, 0]) > 0
    assert qlike_loss([1.0, 1.0], [1.0, 1.0]) == pytest.approx(0.0)


def test_walk_forward_is_temporally_ordered_and_purged():
    folds = purged_walk_forward_folds(100, n_splits=4, test_size=10, label_horizon=2)
    assert len(folds) == 4
    for fold in folds:
        assert max(fold.train_indices) < min(fold.test_indices) - 1


def test_risk_overlay_only_reduces_size_and_respects_floor():
    result = assess_statistical_risk(
        forecast_volatility=0.04,
        reference_volatility=0.02,
        tail_probability=0.2,
        expected_shortfall=0.05,
    )
    assert 0.2 <= result.size_multiplier <= 1.0
    assert result.size_multiplier < 1.0
    assert "elevated_tail_probability" in result.reasons


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "forecast_volatility": -1,
            "reference_volatility": 1,
            "tail_probability": 0.1,
        },
        {
            "forecast_volatility": 1,
            "reference_volatility": 0,
            "tail_probability": 0.1,
        },
        {
            "forecast_volatility": 1,
            "reference_volatility": 1,
            "tail_probability": 1.1,
        },
    ],
)
def test_risk_overlay_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        assess_statistical_risk(**kwargs)
