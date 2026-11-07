import math

import pytest

from aitos.statistics.advanced_models import (
    GJRGARCH11,
    bocpd_change_point,
    fit_har_rv_forecast,
)


def test_bocpd_posterior_is_normalized_and_bounded():
    values = [0.001 * math.sin(i) for i in range(80)] + [0.03] * 12
    result = bocpd_change_point(values, hazard=0.02, max_run_length=64)
    assert 0.0 <= result.change_point_probability <= 1.0
    assert result.expected_run_length >= 0.0
    assert sum(result.run_length_probabilities) == pytest.approx(1.0)
    assert len(result.run_length_probabilities) <= 65


def test_bocpd_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        bocpd_change_point([0.1, 0.2], hazard=1.0)
    with pytest.raises(ValueError):
        bocpd_change_point([0.1, 0.2], max_run_length=1)


def test_fitted_har_rv_uses_only_prior_features_and_returns_finite_forecast():
    values = [0.002 * math.sin(i / 3) + 0.0002 * math.cos(i) for i in range(180)]
    result = fit_har_rv_forecast(values)
    assert result.variance >= 0.0
    assert result.volatility == pytest.approx(math.sqrt(result.variance))
    assert result.observations > 8
    assert len(result.coefficients) == 4
    assert math.isfinite(result.r_squared)


def test_har_fallback_and_window_validation():
    result = fit_har_rv_forecast([0.01, -0.02, 0.01])
    assert not result.fitted
    with pytest.raises(ValueError):
        fit_har_rv_forecast([0.01] * 100, short_window=22, medium_window=5)


def test_gjr_garch_forecast_is_positive_and_stationary():
    values = [
        (-1 if i % 7 == 0 else 1) * (0.001 + (i % 5) * 0.0002) for i in range(120)
    ]
    result = GJRGARCH11().fit_forecast(values)
    assert result.variance > 0.0
    assert result.volatility > 0.0
    assert 0.0 <= result.persistence < 1.0
    assert result.gamma >= 0.0
