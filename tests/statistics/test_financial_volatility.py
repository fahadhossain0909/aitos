import random

import pytest

from aitos.statistics.financial_volatility import (
    estimate_jump_diffusion,
    estimate_rough_volatility,
    estimate_stochastic_volatility,
    financial_volatility_features,
)


def _returns():
    rng = random.Random(19)
    values = [rng.gauss(0.0, 0.002) for _ in range(256)]
    for index in (31, 79, 143, 211):
        values[index] += 0.04 if index % 2 else -0.035
    return values


def test_stochastic_volatility_estimator_is_finite_and_bounded():
    result = estimate_stochastic_volatility(_returns())
    assert result.variance > 0
    assert result.long_run_variance > 0
    assert -0.99 <= result.persistence <= 0.995
    assert result.innovation_volatility >= 0
    assert result.observations == 256


def test_jump_diffusion_estimator_identifies_tail_events():
    result = estimate_jump_diffusion(_returns(), jump_sigma=4.0)
    assert result.jump_count >= 1
    assert 0 < result.jump_intensity_per_observation <= 1
    assert result.diffusion_variance >= 0
    assert result.jump_variance_fraction >= 0


def test_rough_volatility_estimator_returns_hurst_diagnostic():
    result = estimate_rough_volatility(_returns())
    assert 0.01 <= result.hurst_exponent <= 0.99
    assert result.scales_used >= 2
    assert result.rough == (result.hurst_exponent < 0.5)


def test_volatility_feature_adapter_handles_short_history():
    assert financial_volatility_features([0.001] * 10) == {
        "sv_log_variance": 0.0,
        "sv_persistence": 0.0,
        "jump_intensity": 0.0,
        "jump_variance_fraction": 0.0,
        "rough_hurst": 0.5,
    }
    features = financial_volatility_features(_returns())
    assert set(features) == {
        "sv_log_variance",
        "sv_persistence",
        "jump_intensity",
        "jump_variance_fraction",
        "rough_hurst",
    }


def test_estimators_reject_non_finite_or_short_inputs():
    with pytest.raises(ValueError):
        estimate_stochastic_volatility([0.0] * 10)
    with pytest.raises(ValueError):
        estimate_jump_diffusion([float("nan")] * 32)
    with pytest.raises(ValueError):
        estimate_rough_volatility([0.001] * 20)
