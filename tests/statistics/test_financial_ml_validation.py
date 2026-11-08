import pytest

from aitos.statistics.financial_ml_validation import (
    monte_carlo_stress_test,
    validate_strategy_family,
)


def test_monte_carlo_block_bootstrap_is_reproducible_and_cost_sensitive():
    returns = [0.002, 0.001, -0.0005, 0.003] * 12
    baseline = monte_carlo_stress_test(
        returns, paths=200, horizon=40, block_length=4, seed=13
    )
    repeated = monte_carlo_stress_test(
        returns, paths=200, horizon=40, block_length=4, seed=13
    )
    stressed = monte_carlo_stress_test(
        returns,
        paths=200,
        horizon=40,
        block_length=4,
        seed=13,
        additional_cost_bps=10,
    )
    assert baseline == repeated
    assert baseline.paths == 200
    assert 0 <= baseline.probability_of_loss <= 1
    assert baseline.terminal_return_p05 <= baseline.terminal_return_p95
    assert stressed.median_terminal_return < baseline.median_terminal_return


def test_strategy_family_validation_runs_reality_check_fdr_and_mcs():
    returns_a = [0.001 + (index % 7) * 0.0001 for index in range(120)]
    returns_b = [0.0002 - (index % 5) * 0.00005 for index in range(120)]
    report = validate_strategy_family(
        {"primary": returns_a, "challenger": returns_b},
        hypothesis_p_values={"primary_signal": 0.01, "secondary_signal": 0.04},
        model_losses={
            "primary": [1.0 - value for value in returns_a],
            "challenger": [1.0 - value for value in returns_b],
        },
        resamples=100,
        block_length=5,
        seed=7,
    )
    assert report.family_size == 2
    assert report.reality_check.resamples == 100
    assert report.adjusted_p_values is not None
    assert report.adjusted_p_values.method == "bh"
    assert report.model_confidence_set is not None


def test_monte_carlo_and_family_validation_reject_bad_inputs():
    with pytest.raises(ValueError):
        monte_carlo_stress_test([0.01] * 10)
    with pytest.raises(ValueError):
        monte_carlo_stress_test([0.01] * 20, paths=10)
    with pytest.raises(ValueError):
        validate_strategy_family({})
