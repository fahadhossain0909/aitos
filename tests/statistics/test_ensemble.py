import math

import pytest

from aitos.statistics.ensemble import ForecastMember, combine_forecasts


def test_ensemble_includes_within_model_and_between_model_uncertainty():
    result = combine_forecasts(
        [
            ForecastMember("model-a", 1.0, 0.01, 0.04, 0.8),
            ForecastMember("model-b", 1.0, -0.01, 0.04, 0.2),
        ]
    )
    assert result.expected_return == pytest.approx(0.0)
    assert result.variance == pytest.approx(0.04 + 0.0001)
    assert result.volatility == pytest.approx(math.sqrt(0.0401))
    assert result.probability_up == pytest.approx(0.5)
    assert result.return_disagreement == pytest.approx(0.01)


def test_ensemble_normalizes_weights_and_validates_members():
    result = combine_forecasts(
        [
            ForecastMember("model-a", 3.0, 0.02, 0.01, 0.7),
            ForecastMember("model-b", 1.0, 0.00, 0.01, 0.5),
        ]
    )
    assert result.normalized_weights == pytest.approx((0.75, 0.25))
    with pytest.raises(ValueError):
        combine_forecasts([])
    with pytest.raises(ValueError):
        combine_forecasts([ForecastMember("model-a", 0.0, 0.0, 0.1, 0.5)])
    with pytest.raises(ValueError):
        combine_forecasts([ForecastMember("model-a", 1.0, 0.0, -0.1, 0.5)])
