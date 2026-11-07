import pytest

from aitos.statistics.validation import (
    assess_feature_drift,
    paired_block_bootstrap_difference,
    population_stability_index,
)


def test_paired_block_bootstrap_returns_reproducible_oos_interval():
    candidate = [0.001 + (index % 3) * 0.0001 for index in range(60)]
    incumbent = [0.0 for _ in range(60)]
    first = paired_block_bootstrap_difference(
        candidate,
        incumbent,
        block_length=5,
        n_resamples=500,
        seed=17,
    )
    second = paired_block_bootstrap_difference(
        candidate,
        incumbent,
        block_length=5,
        n_resamples=500,
        seed=17,
    )
    assert first == second
    assert first.lower > 0.0
    assert first.lower <= first.estimate <= first.upper
    assert first.sample_count == 60


def test_paired_block_bootstrap_rejects_invalid_samples_and_settings():
    with pytest.raises(ValueError):
        paired_block_bootstrap_difference([0.1], [], n_resamples=100)
    with pytest.raises(ValueError):
        paired_block_bootstrap_difference([0.1], [0.0], block_length=2)
    with pytest.raises(ValueError):
        paired_block_bootstrap_difference([0.1], [0.0], n_resamples=99)
    with pytest.raises(ValueError):
        paired_block_bootstrap_difference([float("nan")], [0.0])


def test_population_stability_index_detects_distribution_shift():
    reference = [index / 100.0 for index in range(100)]
    stable = population_stability_index(reference, reference)
    shifted = population_stability_index(
        reference, [value + 0.5 for value in reference]
    )
    constant_shift = population_stability_index([1.0] * 30, [2.0] * 30)
    assert stable == pytest.approx(0.0)
    assert shifted > 0.2
    assert constant_shift > 0.2


def test_feature_drift_report_identifies_shifted_features():
    reference = {
        "volatility": [index / 100.0 for index in range(100)],
        "imbalance": [0.0, 0.1, 0.2, 0.1] * 25,
    }
    current = {
        "volatility": [index / 100.0 + 0.5 for index in range(100)],
        "imbalance": [0.0, 0.1, 0.2, 0.1] * 25,
    }
    report = assess_feature_drift(reference, current, threshold=0.2)
    assert report.drifted
    assert report.drifted_features == ("volatility",)
    assert report.max_psi > 0.2
    with pytest.raises(ValueError):
        assess_feature_drift(reference, {"other": [1.0]})


def test_execution_cost_sensitivity_includes_fees_funding_and_stress():
    from aitos.statistics.validation import execution_cost_sensitivity

    result = execution_cost_sensitivity(
        0.004,
        entry_fee_bps=4.0,
        exit_fee_bps=4.0,
        spread_bps=2.0,
        slippage_bps=3.0,
        funding_bps=1.0,
        borrow_bps=0.0,
    )
    assert result.base_cost_bps == pytest.approx(14.0)
    assert result.scenarios[0].net_expected_return == pytest.approx(0.0026)
    assert (
        result.scenarios[1].net_expected_return
        < result.scenarios[0].net_expected_return
    )
    assert (
        result.scenarios[2].net_expected_return
        < result.scenarios[1].net_expected_return
    )


def test_execution_cost_sensitivity_rejects_invalid_inputs():
    from aitos.statistics.validation import execution_cost_sensitivity

    with pytest.raises(ValueError):
        execution_cost_sensitivity(
            0.01,
            entry_fee_bps=-1.0,
            exit_fee_bps=0.0,
            spread_bps=0.0,
            slippage_bps=0.0,
        )
    with pytest.raises(ValueError):
        execution_cost_sensitivity(
            0.01,
            entry_fee_bps=1.0,
            exit_fee_bps=1.0,
            spread_bps=1.0,
            slippage_bps=1.0,
            stress_multipliers=(0.5,),
        )
