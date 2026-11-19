"""Purged rolling-origin validation for offline event-impact research.

Each fold fits a fresh cohort model using only labels mature before the
fold's training cutoff, then evaluates later events. Research only: no scanner,
risk, execution, or model-promotion integration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.impact_model import (
    EventImpactEvaluation,
    EventImpactTrainingRow,
    evaluate_event_impact_model,
    fit_event_impact_model,
)


@dataclass(frozen=True, slots=True)
class EventImpactWalkForwardFold:
    fold_index: int
    training_count: int
    test_count: int
    excluded_unseen_event_types: int
    training_as_of: datetime
    test_start: datetime
    test_end: datetime
    evaluation: EventImpactEvaluation


@dataclass(frozen=True, slots=True)
class EventImpactWalkForwardReport:
    asset: str
    horizon: str
    regime: str
    folds: tuple[EventImpactWalkForwardFold, ...]
    skipped_insufficient_training_folds: int
    excluded_outcomes_after_evaluation_cutoff: int


def evaluate_event_impact_walk_forward(
    observations: Sequence[EventImpactTrainingRow],
    *,
    asset: str,
    horizon: str,
    regime: str,
    as_of: datetime,
    min_train_samples: int = 100,
    test_size: int = 25,
    embargo: timedelta = timedelta(0),
    epochs: int = 800,
    learning_rate: float = 0.05,
    regularization: float = 0.1,
) -> EventImpactWalkForwardReport:
    """Run expanding-window folds with maturity purging and unseen-type accounting.

    Training labels must have become available no later than test_start minus
    the embargo. The model is refit from scratch per fold. Test rows with event
    types absent from that fold's training cohort are excluded and counted.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if min_train_samples < 20 or test_size < 1 or embargo < timedelta(0):
        raise ValueError("invalid walk-forward policy")
    cutoff = as_of.astimezone(timezone.utc)
    cohort = [
        row
        for row in observations
        if row.asset.upper() == asset.upper()
        and row.horizon == horizon
        and row.regime == regime
    ]
    ids = [row.event_id for row in cohort]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event_id in walk-forward cohort")
    ordered = sorted(cohort, key=lambda row: (row.event_time, row.event_id))
    folds: list[EventImpactWalkForwardFold] = []
    skipped = 0
    excluded_after_cutoff = 0
    fold_index = 0

    start_index = min_train_samples
    while start_index < len(ordered):
        test_rows = ordered[start_index : start_index + test_size]
        if not test_rows:
            break
        test_start = test_rows[0].event_time
        test_end = test_rows[-1].event_time
        training_as_of = test_start - embargo
        training_rows = [
            row
            for row in ordered[:start_index]
            if row.event_time < training_as_of
            and row.outcome_available_at <= training_as_of
        ]
        if len(training_rows) < min_train_samples:
            skipped += 1
            start_index += test_size
            continue

        model = fit_event_impact_model(
            training_rows,
            asset=asset,
            horizon=horizon,
            regime=regime,
            as_of=training_as_of,
            min_samples=min_train_samples,
            epochs=epochs,
            learning_rate=learning_rate,
            regularization=regularization,
        )
        eligible_test = [
            row
            for row in test_rows
            if row.event_time <= cutoff and row.outcome_available_at <= cutoff
        ]
        excluded_after_cutoff += len(test_rows) - len(eligible_test)
        known_type_rows = [
            row for row in eligible_test if row.event_type in model.event_types
        ]
        unseen_count = len(eligible_test) - len(known_type_rows)
        evaluation = evaluate_event_impact_model(model, known_type_rows, as_of=cutoff)
        folds.append(
            EventImpactWalkForwardFold(
                fold_index=fold_index,
                training_count=model.training_count,
                test_count=len(test_rows),
                excluded_unseen_event_types=unseen_count,
                training_as_of=training_as_of,
                test_start=test_start,
                test_end=test_end,
                evaluation=evaluation,
            )
        )
        fold_index += 1
        start_index += test_size

    return EventImpactWalkForwardReport(
        asset=asset.upper(),
        horizon=horizon,
        regime=regime,
        folds=tuple(folds),
        skipped_insufficient_training_folds=skipped,
        excluded_outcomes_after_evaluation_cutoff=excluded_after_cutoff,
    )


__all__ = [
    "EventImpactWalkForwardFold",
    "EventImpactWalkForwardReport",
    "evaluate_event_impact_walk_forward",
]
