import math

import pytest

from aitos.statistics.selection import (
    correct_multiple_testing,
    deflated_sharpe_ratio,
    diebold_mariano_test,
    model_confidence_set,
    probabilistic_sharpe_ratio,
    white_reality_check,
)


def test_psr_and_dsr_are_finite_and_dsr_accounts_for_trials():
    returns = [0.001 + 0.01 * math.sin(i * 0.37) for i in range(500)]
    psr = probabilistic_sharpe_ratio(returns)
    dsr = deflated_sharpe_ratio(returns, trials=50)
    assert 0.0 <= psr.probability <= 1.0
    assert 0.0 <= dsr.probability <= psr.probability
    assert dsr.benchmark > 0.0


def test_diebold_mariano_detects_paired_loss_difference():
    baseline = [1.0 + 0.1 * math.sin(i) for i in range(300)]
    challenger = [value - 0.15 for value in baseline]
    result = diebold_mariano_test(challenger, baseline)
    assert result.statistic < 0.0
    assert result.p_value < 0.01


def test_reality_check_and_mcs_are_seeded_and_reproducible():
    first = [0.001 + 0.01 * math.sin(i * 0.2) for i in range(240)]
    second = [0.0001 + 0.01 * math.cos(i * 0.17) for i in range(240)]
    panel = {"candidate": first, "alternative": second}
    a = white_reality_check(panel, resamples=150, block_length=4, seed=9)
    b = white_reality_check(panel, resamples=150, block_length=4, seed=9)
    assert a == b
    assert 0.0 < a.p_value <= 1.0
    mcs = model_confidence_set(
        {"good": [-x for x in first], "weak": [-x for x in second]},
        resamples=150,
        block_length=4,
        seed=9,
    )
    assert mcs.included
    assert set(mcs.included).issubset({"good", "weak"})


def test_multiple_testing_holm_and_bh_are_monotone_and_validated():
    values = {"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.8}
    holm = correct_multiple_testing(values, method="holm")
    bh = correct_multiple_testing(values, method="bh")
    assert holm.adjusted["a"] >= values["a"]
    assert bh.adjusted["a"] <= holm.adjusted["a"]
    assert "a" in holm.rejected
    with pytest.raises(ValueError):
        correct_multiple_testing({"bad": 1.2})
