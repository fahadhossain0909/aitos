from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.impact_model import (
    EventImpactFeatures,
    EventImpactTrainingRow,
    evaluate_event_impact_model,
    fit_event_impact_model,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(
    index, *, positive=None, available=True, asset="BTCUSDT", regime="trend"
):
    signal = (index % 10 - 4.5) / 5.0
    positive = signal > 0 if positive is None else positive
    event_time = BASE + timedelta(hours=index)
    return EventImpactTrainingRow(
        event_id=f"event-{index}",
        asset=asset,
        horizon="1h",
        regime=regime,
        event_type="macro" if index % 2 else "exchange",
        event_time=event_time,
        outcome_available_at=event_time + timedelta(hours=1 if available else 1000),
        sentiment=signal,
        severity=0.7,
        novelty=0.6,
        confidence=0.8,
        source_reliability=0.9,
        age_hours=1.0,
        beta=0.2,
        forward_return=0.01 if positive else -0.01,
    )


def fit(rows):
    return fit_event_impact_model(
        rows,
        asset="BTCUSDT",
        horizon="1h",
        regime="trend",
        as_of=BASE + timedelta(days=30),
        min_samples=20,
        epochs=100,
    )


def test_model_fits_only_matured_rows_and_predicts_probability():
    rows = [row(i) for i in range(40)]
    rows.append(row(41, positive=False, available=False))
    model = fit(rows)
    assert model.training_count == 40
    features = EventImpactFeatures(
        asset="BTCUSDT",
        horizon="1h",
        regime="trend",
        event_type="macro",
        sentiment=0.8,
        severity=0.7,
        novelty=0.6,
        confidence=0.8,
        source_reliability=0.9,
        age_hours=1.0,
        beta=0.2,
    )
    probability = model.predict_probability_up(features)
    assert 0.0 < probability < 1.0


def test_model_rejects_cross_cohort_prediction():
    model = fit([row(i) for i in range(40)])
    features = EventImpactFeatures(
        asset="ETHUSDT",
        horizon="1h",
        regime="trend",
        event_type="macro",
        sentiment=0.5,
        severity=0.5,
        novelty=0.5,
        confidence=0.5,
        source_reliability=0.5,
        age_hours=1,
        beta=0.1,
    )
    with pytest.raises(ValueError, match="cohort"):
        model.predict_probability_up(features)


def test_model_rejects_insufficient_history_and_single_class():
    with pytest.raises(ValueError, match="insufficient"):
        fit([row(i) for i in range(19)])
    with pytest.raises(ValueError, match="both positive and negative"):
        fit([row(i, positive=True) for i in range(40)])


def test_model_rejects_duplicate_events_and_future_outcomes():
    rows = [row(i) for i in range(40)]
    rows.append(row(0))
    with pytest.raises(ValueError, match="duplicate"):
        fit(rows)
    future = [row(i, available=False) for i in range(40)]
    with pytest.raises(ValueError, match="insufficient"):
        fit(future)


def test_training_row_rejects_invalid_numeric_inputs():
    with pytest.raises(ValueError, match="sentiment"):
        EventImpactTrainingRow(
            event_id="bad",
            asset="BTCUSDT",
            horizon="1h",
            regime="trend",
            event_type="macro",
            event_time=BASE,
            outcome_available_at=BASE + timedelta(hours=1),
            sentiment=2.0,
            severity=0.5,
            novelty=0.5,
            confidence=0.5,
            source_reliability=0.5,
            age_hours=1,
            beta=0,
            forward_return=0.01,
        )


def test_oos_evaluation_excludes_training_rows_and_unmatured_labels():
    training = [row(i) for i in range(40)]
    model = fit(training)
    oos = [row(800 + i) for i in range(10)]
    oos.append(row(900, available=False))
    report = evaluate_event_impact_model(
        model, training + oos, as_of=BASE + timedelta(days=40)
    )
    assert report.sample_count == 10
    assert report.excluded_pre_training == 40
    assert report.excluded_unmatured == 1
    assert 0.0 <= report.brier_score <= 1.0
    assert 0.0 <= report.directional_accuracy <= 1.0


def test_oos_evaluation_rejects_duplicate_ids_and_earlier_cutoff():
    model = fit([row(i) for i in range(40)])
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_event_impact_model(
            model,
            [row(800), row(800)],
            as_of=BASE + timedelta(days=40),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        evaluate_event_impact_model(model, [], as_of=BASE + timedelta(days=1))


def test_model_rejects_unseen_event_type():
    model = fit([row(i) for i in range(40)])
    features = EventImpactFeatures(
        asset="BTCUSDT",
        horizon="1h",
        regime="trend",
        event_type="unseen",
        sentiment=0.5,
        severity=0.5,
        novelty=0.5,
        confidence=0.5,
        source_reliability=0.5,
        age_hours=1,
        beta=0.1,
    )
    with pytest.raises(ValueError, match="event_type"):
        model.predict_probability_up(features)
