from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.evaluation import (
    EventPrediction,
    chronological_walk_forward_splits,
    compare_ablation,
    evaluate_predictions,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def prediction(index, probability=0.7, result=0.01, cost=2.0):
    event_time = BASE + timedelta(hours=index)
    return EventPrediction(
        event_id=str(index),
        asset="BTCUSDT",
        horizon="1h",
        event_time=event_time,
        outcome_available_at=event_time + timedelta(hours=1),
        predicted_probability_up=probability,
        forward_return=result,
        transaction_cost_bps=cost,
        latency_slippage_bps=1.0,
    )


def test_evaluation_excludes_outcomes_not_available_at_as_of():
    items = [prediction(0), prediction(2)]
    metrics = evaluate_predictions(items, as_of=BASE + timedelta(hours=2))
    assert metrics.sample_count == 1
    assert metrics.mean_net_return == pytest.approx(0.0097)


def test_evaluation_includes_fees_funding_and_latency_slippage():
    item = EventPrediction(
        event_id="costs",
        asset="BTCUSDT",
        horizon="1h",
        event_time=BASE,
        outcome_available_at=BASE + timedelta(hours=1),
        predicted_probability_up=0.9,
        forward_return=0.001,
        transaction_cost_bps=5.0,
        funding_cost_bps=2.0,
        latency_slippage_bps=4.0,
    )
    metrics = evaluate_predictions([item], as_of=BASE + timedelta(hours=2))
    assert metrics.mean_net_return == pytest.approx(-0.0001)


def test_walk_forward_splits_purge_labels_that_overlap_test_start():
    items = [prediction(index) for index in range(10)]
    splits = chronological_walk_forward_splits(
        items, min_train_samples=4, test_size=2, embargo=timedelta(minutes=30)
    )
    assert splits
    for split in splits:
        assert all(
            item.outcome_available_at <= split.test_start - timedelta(minutes=30)
            for item in split.train
        )
        assert max(item.event_time for item in split.train) < split.test_start


def test_ablation_uses_only_matched_events_and_reports_incremental_net_return():
    baseline = [prediction(0), prediction(1), prediction(2)]
    enriched = [
        prediction(0, probability=0.8, result=0.02),
        prediction(1, probability=0.8, result=0.01),
        prediction(99, probability=0.9, result=0.5),
    ]
    result = compare_ablation(baseline, enriched, as_of=BASE + timedelta(days=1))
    assert result.matched_sample_count == 2
    assert result.baseline.sample_count == 2
    assert result.with_intelligence.sample_count == 2
    assert result.incremental_mean_net_return is not None


def test_evaluation_rejects_naive_as_of_and_invalid_probability():
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_predictions([], as_of=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="predicted_probability_up"):
        EventPrediction(
            event_id="bad",
            asset="BTC",
            horizon="1h",
            event_time=BASE,
            outcome_available_at=BASE,
            predicted_probability_up=1.5,
            forward_return=0.0,
        )


def test_ablation_rejects_mismatched_realized_outcomes():
    baseline = [prediction(0)]
    enriched = [prediction(0, result=0.5)]
    with pytest.raises(ValueError, match="identical outcomes"):
        compare_ablation(baseline, enriched, as_of=BASE + timedelta(days=1))
