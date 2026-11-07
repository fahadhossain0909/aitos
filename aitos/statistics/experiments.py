"""Chronological symbol/regime OOS experiment orchestration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .validation import (
    CohortValidationMetric,
    purged_walk_forward_folds,
    regime_symbol_validation,
)


@dataclass(frozen=True)
class ExperimentObservation:
    timestamp: int
    symbol: str
    regime: str
    features: tuple[float, ...]
    outcome: int
    label_end_timestamp: int

    def __post_init__(self) -> None:
        if self.timestamp < 0 or self.label_end_timestamp < self.timestamp:
            raise ValueError("observation and label-end timestamps must be ordered")
        if not self.symbol.strip() or not self.regime.strip():
            raise ValueError("symbol and regime are required")
        if self.outcome not in (0, 1):
            raise ValueError("experiment outcome must be binary")
        if not self.features or any(not math.isfinite(float(x)) for x in self.features):
            raise ValueError("features must be non-empty and finite")


@dataclass(frozen=True)
class OOSPrediction:
    timestamp: int
    symbol: str
    regime: str
    probability: float
    outcome: int
    fold: int


@dataclass(frozen=True)
class ExperimentCohortSummary:
    symbol: str
    regime: str
    observations: int
    folds_completed: int
    folds_skipped: int


@dataclass(frozen=True)
class SymbolRegimeExperimentResult:
    predictions: tuple[OOSPrediction, ...]
    cohort_summaries: tuple[ExperimentCohortSummary, ...]
    validation_metrics: tuple[CohortValidationMetric, ...]
    total_folds: int
    skipped_folds: int


Predictor = Callable[
    [Sequence[ExperimentObservation], Sequence[ExperimentObservation], str, str],
    Sequence[float],
]


def run_symbol_regime_walk_forward(
    observations: Iterable[ExperimentObservation],
    predictor: Predictor,
    *,
    n_splits: int = 5,
    test_size: int | None = None,
    label_horizon: int = 1,
    embargo: int = 0,
    minimum_train_observations: int = 30,
    minimum_cohort_observations: int = 30,
) -> SymbolRegimeExperimentResult:
    """Fit each symbol/regime cohort on past rows and score only future rows.

    The predictor callback must fit exclusively on train_rows and return one
    probability per test row. Training labels are additionally checked against
    the first test timestamp, so variable-duration labels cannot cross the
    test boundary even when a positional purge is supplied.
    """
    rows = tuple(observations)
    if not rows:
        raise ValueError("at least one experiment observation is required")
    if minimum_train_observations <= 0 or minimum_cohort_observations <= 0:
        raise ValueError("minimum cohort and training sizes must be positive")
    if n_splits <= 0 or label_horizon < 0 or embargo < 0:
        raise ValueError("invalid walk-forward configuration")
    cohorts: dict[tuple[str, str], list[ExperimentObservation]] = {}
    for row in rows:
        if not isinstance(row, ExperimentObservation):
            raise TypeError("observations must be ExperimentObservation values")
        cohorts.setdefault((row.symbol.upper(), row.regime), []).append(row)

    predictions: list[OOSPrediction] = []
    summaries: list[ExperimentCohortSummary] = []
    total_folds = skipped_folds = 0
    for (symbol, regime), cohort in sorted(cohorts.items()):
        cohort.sort(key=lambda row: row.timestamp)
        if any(
            left.timestamp >= right.timestamp for left, right in zip(cohort, cohort[1:])
        ):
            raise ValueError(f"duplicate or unordered timestamps in {symbol}/{regime}")
        if len(cohort) < minimum_cohort_observations:
            summaries.append(ExperimentCohortSummary(symbol, regime, len(cohort), 0, 0))
            continue
        folds = purged_walk_forward_folds(
            len(cohort),
            n_splits=n_splits,
            test_size=test_size,
            label_horizon=label_horizon,
            embargo=embargo,
        )
        completed = skipped = 0
        for fold_number, fold in enumerate(folds):
            total_folds += 1
            train_rows = tuple(cohort[index] for index in fold.train_indices)
            test_rows = tuple(cohort[index] for index in fold.test_indices)
            if not test_rows:
                skipped += 1
                skipped_folds += 1
                continue
            test_start = test_rows[0].timestamp
            train_rows = tuple(
                row for row in train_rows if row.label_end_timestamp < test_start
            )
            if len(train_rows) < minimum_train_observations:
                skipped += 1
                skipped_folds += 1
                continue
            probabilities = tuple(
                float(value)
                for value in predictor(train_rows, test_rows, symbol, regime)
            )
            if len(probabilities) != len(test_rows) or any(
                not math.isfinite(value) or not 0.0 <= value <= 1.0
                for value in probabilities
            ):
                raise ValueError(
                    "predictor must return aligned probabilities in [0, 1]"
                )
            predictions.extend(
                OOSPrediction(
                    timestamp=row.timestamp,
                    symbol=symbol,
                    regime=regime,
                    probability=probability,
                    outcome=row.outcome,
                    fold=fold_number,
                )
                for row, probability in zip(test_rows, probabilities)
            )
            completed += 1
        summaries.append(
            ExperimentCohortSummary(symbol, regime, len(cohort), completed, skipped)
        )
    if not predictions:
        raise ValueError("no out-of-sample predictions were produced")
    metrics = regime_symbol_validation(
        [row.probability for row in predictions],
        [row.outcome for row in predictions],
        symbols=[row.symbol for row in predictions],
        regimes=[row.regime for row in predictions],
        minimum_observations=minimum_cohort_observations,
    )
    return SymbolRegimeExperimentResult(
        tuple(
            sorted(predictions, key=lambda row: (row.timestamp, row.symbol, row.regime))
        ),
        tuple(summaries),
        metrics,
        total_folds,
        skipped_folds,
    )
