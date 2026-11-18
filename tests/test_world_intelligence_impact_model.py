from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.impact_model import (
    EventImpactFeatures,
    EventImpactTrainingRow,
    fit_event_impact_model,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(index, *, positive=None, available=True, asset="BTCUSDT", regime="trend"):
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
        outcome_available_at=event_time + timedelta(hours=1 if available else 100),
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
    bad = row(0)
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
