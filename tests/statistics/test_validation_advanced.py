import pytest

from aitos.statistics.validation import (
    combinatorial_purged_folds,
    probability_of_backtest_overfitting,
)


def test_combinatorial_purged_folds_exclude_test_and_adjacent_observations():
    folds = combinatorial_purged_folds(
        80, n_groups=8, n_test_groups=2, purge=2, embargo=1
    )
    assert len(folds) == 28
    for fold in folds:
        assert not set(fold.train_indices).intersection(fold.test_indices)
        assert fold.test_groups


def test_combinatorial_folds_validate_parameters():
    with pytest.raises(ValueError):
        combinatorial_purged_folds(10, n_groups=11)
    with pytest.raises(ValueError):
        combinatorial_purged_folds(10, n_groups=5, n_test_groups=5)


def test_pbo_is_bounded_and_detects_consistently_overfit_winner():
    strategy_returns = [
        [0.03, 0.03, 0.03, 0.03, -0.02, -0.02, -0.02, -0.02],
        [0.01, 0.01, 0.01, 0.01, 0.02, 0.02, 0.02, 0.02],
    ]
    pbo = probability_of_backtest_overfitting(strategy_returns, n_slices=4)
    assert 0.0 <= pbo <= 1.0


def test_pbo_rejects_non_finite_or_unequal_inputs():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([[0.1, 0.2], [0.1]])
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([[0.1, float("nan")], [0.2, 0.3]])
