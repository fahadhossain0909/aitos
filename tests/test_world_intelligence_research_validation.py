from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.research_validation import OOSPrediction, evaluate_regime_oos

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_rows(count=80):
    rows = []
    for i in range(count):
        label = i % 2 == 0
        event_time = BASE + timedelta(hours=i + 1)
        rows.append(OOSPrediction(
            event_id=f"event-{i}", regime="trend" if i < 40 else "range",
            event_time=event_time,
            label_available_at=event_time + timedelta(hours=2 if i != 3 else 100),
            candidate_probability=0.9 if label else 0.1,
            benchmark_probability=0.6 if label else 0.4,
            label_up=label,
        ))
    return rows


def test_oos_reports_candidate_vs_benchmark_with_regime_confidence_intervals():
    report = evaluate_regime_oos(
        make_rows(), as_of=BASE + timedelta(days=10),
        training_cutoff=BASE, min_samples=20, iterations=300,
    )
    assert len(report.reports) == 2
    assert report.excluded_unmatured == 1
    for regime in report.reports:
        assert regime.sample_count >= 39
        assert regime.candidate_brier < regime.benchmark_brier
        assert regime.paired_improvement_ci[0] > 0
        assert regime.directional_accuracy_ci[0] > 0.8


def test_oos_validation_rejects_in_sample_or_duplicate_predictions():
    row = make_rows(4)[0]
    with pytest.raises(ValueError, match="strictly after"):
        evaluate_regime_oos([row], as_of=BASE + timedelta(days=2),
                            training_cutoff=row.event_time, min_samples=3, iterations=100)
    rows = make_rows(4)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_regime_oos([rows[0], rows[0]], as_of=BASE + timedelta(days=2),
                            training_cutoff=BASE, min_samples=3, iterations=100)
