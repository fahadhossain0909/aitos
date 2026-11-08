import pytest

from aitos.statistics.financial_ml import (
    BarrierObservation,
    MarketEvent,
    MetaLabeler,
    build_dollar_bars,
    build_imbalance_bars,
    build_volume_bars,
    fractional_difference,
    fractional_difference_weights,
    sequential_bootstrap_indices,
    triple_barrier_labels,
)


def test_volume_dollar_and_imbalance_bars_close_only_complete_bars():
    events = [
        MarketEvent("2026-01-01T00:00:00Z", 10, 2, 1),
        MarketEvent("2026-01-01T00:00:01Z", 11, 3, -3),
        MarketEvent("2026-01-01T00:00:02Z", 12, 1, 1),
    ]
    volume = build_volume_bars(events, volume_threshold=4)
    assert len(volume) == 1
    assert volume[0].event_count == 2
    assert volume[0].volume == 5
    assert build_dollar_bars(events, dollar_threshold=50)[0].event_count == 2
    assert build_imbalance_bars(events, imbalance_threshold=2)[0].event_count == 2


def test_event_bars_reject_bad_order_and_threshold():
    events = [
        MarketEvent("2026-01-01T00:00:01Z", 10, 1, 1),
        MarketEvent("2026-01-01T00:00:00Z", 10, 1, 1),
    ]
    with pytest.raises(ValueError):
        build_volume_bars(events, volume_threshold=1)
    with pytest.raises(ValueError):
        build_volume_bars([], volume_threshold=0)


def test_fractional_difference_is_causal_and_validated():
    weights = fractional_difference_weights(0.4, threshold=0.01)
    assert weights[0] == 1.0
    values = fractional_difference(range(100), order=0.4, threshold=0.01)
    first_valid = next(index for index, value in enumerate(values) if value is not None)
    assert first_valid == len(weights) - 1
    assert values[-1] is not None
    with pytest.raises(ValueError):
        fractional_difference([1, 2], order=1.0)


def _bars():
    return [
        BarrierObservation(f"2026-01-01T00:00:{i:02d}Z", 100, high, low, close)
        for i, (high, low, close) in enumerate(
            [
                (100, 100, 100),
                (103, 100, 102),
                (102, 100, 101),
                (101, 99, 100),
            ]
        )
    ]


def test_triple_barrier_labels_long_and_short_with_costs():
    bars = _bars()
    labels = triple_barrier_labels(
        bars,
        event_indices=[0, 2],
        sides=[1, -1],
        volatility=[0.01] * len(bars),
        profit_take=2,
        stop_loss=1,
        max_holding_bars=2,
        entry_fee_bps=2,
        exit_fee_bps=2,
    )
    assert labels[0].barrier == "take"
    assert labels[0].label == 1
    assert labels[0].net_return < labels[0].gross_return
    assert labels[1].barrier in {"stop", "take", "vertical"}


def test_triple_barrier_uses_conservative_stop_when_both_hit():
    bars = [
        BarrierObservation("t0", 100, 100, 100, 100),
        BarrierObservation("t1", 100, 102, 98, 99),
    ]
    label = triple_barrier_labels(
        bars,
        event_indices=[0],
        sides=[1],
        volatility=[0.01, 0.01],
        profit_take=1,
        stop_loss=1,
        max_holding_bars=1,
    )[0]
    assert label.barrier == "stop"
    assert label.label == -1


def test_meta_labeler_fits_predicts_and_never_accepts_by_default_on_unfitted():
    model = MetaLabeler(learning_rate=0.1, epochs=500)
    features = [[-2.0], [-1.0], [1.0], [2.0]]
    labels = [0, 0, 1, 1]
    model.fit(features, labels)
    probabilities = model.predict_probability(features)
    assert probabilities[0] < probabilities[-1]
    predictions = model.predict(features, threshold=0.5)
    assert not predictions[0].accepted
    assert predictions[-1].accepted
    with pytest.raises(RuntimeError):
        MetaLabeler().predict_probability([[1.0]])


def test_meta_labeler_rejects_single_class_and_bad_feature_width():
    with pytest.raises(ValueError):
        MetaLabeler().fit([[1.0], [2.0]], [1, 1])
    model = MetaLabeler().fit([[0.0], [1.0]], [0, 1])
    with pytest.raises(ValueError):
        model.predict_probability([[1.0, 2.0]])


def test_sequential_bootstrap_is_reproducible_and_validates_intervals():
    first = sequential_bootstrap_indices([0, 0, 5], [5, 5, 7], sample_count=20, seed=7)
    second = sequential_bootstrap_indices([0, 0, 5], [5, 5, 7], sample_count=20, seed=7)
    assert first == second
    assert len(first) == 20
    with pytest.raises(ValueError):
        sequential_bootstrap_indices([2], [1])


def test_triple_barrier_gap_stop_uses_open_price_not_barrier_price():
    bars = [
        BarrierObservation("t0", 100, 100, 100, 100),
        BarrierObservation("t1", 97, 98, 96, 97),
    ]
    label = triple_barrier_labels(
        bars,
        event_indices=[0],
        sides=[1],
        volatility=[0.01, 0.01],
        profit_take=2,
        stop_loss=1,
        max_holding_bars=1,
    )[0]
    assert label.barrier == "stop"
    assert label.gross_return == pytest.approx(-0.03)
