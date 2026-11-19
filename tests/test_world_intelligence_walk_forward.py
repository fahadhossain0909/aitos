from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.impact_model import EventImpactTrainingRow
from aitos.world_intelligence.walk_forward import evaluate_event_impact_walk_forward

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(index, *, event_type=None, available_hours=1):
    signal = (index % 10 - 4.5) / 5.0
    event_time = BASE + timedelta(hours=index)
    return EventImpactTrainingRow(
        event_id=f"event-{index}",
        asset="BTCUSDT",
        horizon="1h",
        regime="trend",
        event_type=event_type or ("macro" if index % 2 else "exchange"),
        event_time=event_time,
        outcome_available_at=event_time + timedelta(hours=available_hours),
        sentiment=signal,
        severity=0.7,
        novelty=0.6,
        confidence=0.8,
        source_reliability=0.9,
        age_hours=1.0,
        beta=0.2,
        forward_return=0.01 if signal > 0 else -0.01,
    )


def run(rows, *, embargo=timedelta(0), as_of=None):
    return evaluate_event_impact_walk_forward(
        rows,
        asset="BTCUSDT",
        horizon="1h",
        regime="trend",
        as_of=as_of or BASE + timedelta(hours=200),
        min_train_samples=40,
        test_size=20,
        embargo=embargo,
        epochs=30,
    )


def test_walk_forward_refits_each_fold_and_purges_by_label_maturity():
    report = run([row(index) for index in range(120)])
    assert len(report.folds) == 4
    for fold in report.folds:
        assert fold.training_count >= 40
        assert fold.training_as_of == fold.test_start
        assert fold.evaluation.sample_count == fold.test_count
        assert fold.evaluation.brier_score is not None


def test_embargo_removes_overlapping_training_labels():
    rows = [row(index) for index in range(120)]
    report = run(rows, embargo=timedelta(hours=2))
    assert report.folds
    for fold in report.folds:
        assert fold.training_as_of == fold.test_start - timedelta(hours=2)
        expected_matured_history = int(
            (fold.test_start - BASE).total_seconds() // 3600
        ) - 2
        assert fold.training_count == expected_matured_history


def test_unseen_event_types_are_excluded_and_counted():
    rows = [row(index) for index in range(80)]
    rows[40] = row(40, event_type="new_geopolitical_category")
    report = run(rows)
    assert report.folds[0].excluded_unseen_event_types == 1
    assert report.folds[0].evaluation.sample_count == 19


def test_unmatured_test_labels_are_excluded_at_evaluation_cutoff():
    rows = [row(index) for index in range(80)]
    rows[45] = row(45, available_hours=100)
    report = run(rows, as_of=BASE + timedelta(hours=60))
    assert report.folds[0].evaluation.sample_count == 19
    assert report.excluded_outcomes_after_evaluation_cutoff >= 1


def test_duplicate_ids_invalid_policy_and_naive_cutoff_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        run([row(index) for index in range(60)] + [row(0)])
    with pytest.raises(ValueError, match="invalid"):
        evaluate_event_impact_walk_forward(
            [],
            asset="BTCUSDT",
            horizon="1h",
            regime="trend",
            as_of=BASE,
            min_train_samples=10,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_event_impact_walk_forward(
            [],
            asset="BTCUSDT",
            horizon="1h",
            regime="trend",
            as_of=datetime(2026, 10, 1),
        )
