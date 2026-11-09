import pytest

from aitos.statistics.experiments import (
    ExperimentObservation,
    run_symbol_regime_walk_forward,
)


def _panel():
    rows = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for regime in ("quiet", "volatile"):
            for timestamp in range(120):
                signal = (timestamp % 7) / 7.0
                outcome = int(signal > 0.45)
                rows.append(
                    ExperimentObservation(
                        timestamp=timestamp,
                        symbol=symbol,
                        regime=regime,
                        features=(signal, float(timestamp % 5)),
                        outcome=outcome,
                        label_end_timestamp=timestamp + 1,
                    )
                )
    return rows


def test_symbol_regime_orchestrator_is_purged_and_oos_only():
    calls = []

    def predictor(train_rows, test_rows, symbol, regime):
        assert max(row.timestamp for row in train_rows) < min(
            row.timestamp for row in test_rows
        )
        assert max(row.label_end_timestamp for row in train_rows) < min(
            row.timestamp for row in test_rows
        )
        calls.append((symbol, regime, len(train_rows), len(test_rows)))
        return [0.8 if row.features[0] > 0.45 else 0.2 for row in test_rows]

    result = run_symbol_regime_walk_forward(
        _panel(),
        predictor,
        n_splits=3,
        test_size=20,
        label_horizon=2,
        embargo=2,
        minimum_train_observations=30,
        minimum_cohort_observations=30,
    )
    assert len(calls) == 12
    assert result.total_folds == 12
    assert result.skipped_folds == 0
    assert len(result.predictions) == 240
    assert {row.symbol for row in result.predictions} == {"BTCUSDT", "ETHUSDT"}
    assert {row.regime for row in result.predictions} == {"quiet", "volatile"}
    assert result.validation_metrics


def test_symbol_regime_orchestrator_rejects_invalid_predictor_probabilities():
    def predictor(train_rows, test_rows, symbol, regime):
        return [1.5] * len(test_rows)

    with pytest.raises(ValueError, match="probabilities"):
        run_symbol_regime_walk_forward(
            _panel(),
            predictor,
            n_splits=2,
            test_size=20,
            label_horizon=2,
            embargo=2,
            minimum_train_observations=20,
        )
