from datetime import datetime, timedelta, timezone

import pytest

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.replay import (
    PriceObservation,
    ReplayRow,
    build_point_in_time_replay,
    chronological_split,
    summarize_replay,
    summarize_replay_by_event_type,
    evaluate_probabilistic_predictions,
    walk_forward_splits,
)


def _event(event_id, at, *, direction=1, finality="confirmed", usd_value=1000):
    return CapitalEvent(
        event_id,
        "fixture",
        "capital_flow",
        "crypto_spot",
        "binance",
        "BTCUSDT",
        "large_transfer",
        at,
        at + timedelta(seconds=1),
        1,
        "BTC",
        usd_value,
        actor_id="wallet-a",
        finality=finality,
        metadata={"flow_sign": direction},
    )


def test_replay_uses_only_point_in_time_prices_and_directional_labels():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _event("in", start),
        _event("out", start + timedelta(seconds=600), direction=-1),
    ]
    prices = [
        PriceObservation("BTCUSDT", start, 100),
        PriceObservation("BTCUSDT", start + timedelta(seconds=300), 110),
        PriceObservation("BTCUSDT", start + timedelta(seconds=600), 120),
        PriceObservation("BTCUSDT", start + timedelta(seconds=900), 108),
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(300,), max_price_age_seconds=1
    )
    assert len(rows) == 2
    assert rows[0].forward_returns[300] == pytest.approx(0.1)
    assert rows[1].forward_returns[300] == pytest.approx(0.1)
    summary = summarize_replay(rows, horizon_seconds=300)
    assert summary.sample_count == 2
    assert summary.hit_rate == 1.0


def test_replay_excludes_reverted_unknown_direction_and_stale_prices():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _event("reverted", start, finality="reverted"),
        _event("unknown", start + timedelta(seconds=1), direction=0),
        _event("stale", start + timedelta(seconds=10)),
    ]
    prices = [
        PriceObservation("BTCUSDT", start, 100),
        PriceObservation("BTCUSDT", start + timedelta(seconds=300), 101),
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(300,), max_price_age_seconds=1
    )
    assert rows == []


def test_chronological_split_keeps_future_rows_out_of_train():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [_event(str(i), start + timedelta(minutes=i)) for i in range(10)]
    prices = [
        PriceObservation("BTCUSDT", start + timedelta(minutes=i), 100 + i)
        for i in range(11)
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(60,), max_price_age_seconds=1
    )
    train, test = chronological_split(rows, test_fraction=0.2)
    assert train and test
    assert max(row.event_time for row in train) < min(row.event_time for row in test)


def test_chronological_split_purges_training_labels_that_overlap_test_period():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(10):
        at = start + timedelta(minutes=index)
        rows.append(
            type(
                "Row",
                (),
                {
                    "event_id": str(index),
                    "event_time": at,
                    "forward_returns": {300: 0.01},
                },
            )()
        )
    train, test = chronological_split(rows, test_fraction=0.2)
    test_start = min(row.event_time for row in test)
    assert train and test
    assert all(
        row.event_time + timedelta(seconds=max(row.forward_returns) + 30) < test_start
        for row in train
    )


def test_chronological_split_supports_explicit_embargo_and_validates_it():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        type(
            "Row",
            (),
            {
                "event_id": str(index),
                "event_time": start + timedelta(minutes=index),
                "forward_returns": {60: 0.01},
            },
        )()
        for index in range(10)
    ]
    train, test = chronological_split(rows, test_fraction=0.2, embargo_seconds=120)
    test_start = min(row.event_time for row in test)
    assert all(row.event_time + timedelta(seconds=210) < test_start for row in train)
    with pytest.raises(ValueError):
        chronological_split(rows, embargo_seconds=-1)


def test_replay_summary_reports_gross_and_net_of_execution_costs():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = build_point_in_time_replay(
        [_event("costed", start)],
        [
            PriceObservation("BTCUSDT", start, 100),
            PriceObservation("BTCUSDT", start + timedelta(seconds=300), 100.2),
        ],
        horizons_seconds=(300,),
        max_price_age_seconds=1,
    )
    summary = summarize_replay(
        rows,
        horizon_seconds=300,
        fee_bps_per_side=5,
        spread_bps_round_trip=5,
        slippage_bps_per_side=5,
        funding_bps_per_8h=8,
    )
    assert summary.mean_return == pytest.approx(0.002)
    assert summary.total_cost_bps == pytest.approx(25.0833333333)
    assert summary.net_mean_return == pytest.approx(0.002 - 25.0833333333 / 10_000)
    assert summary.net_hit_rate == 0.0


def test_replay_summary_rejects_invalid_cost_assumptions():
    with pytest.raises(ValueError):
        summarize_replay([], horizon_seconds=300, fee_bps_per_side=-1)



def test_walk_forward_splits_are_expanding_and_purged():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        ReplayRow(
            event_id=str(index),
            instrument="BTCUSDT",
            event_time=start + timedelta(minutes=index),
            event_type="transfer",
            direction=1,
            flow_usd=1000,
            forward_returns={60: 0.01},
        )
        for index in range(30)
    ]
    folds = walk_forward_splits(rows, n_splits=3, max_quote_age_seconds=30)
    assert len(folds) == 3
    assert len(folds[0][1]) == 5
    assert len(folds[1][0]) > len(folds[0][0])
    for train, test in folds:
        test_start = min(row.event_time for row in test)
        assert all(
            row.event_time + timedelta(seconds=90) < test_start for row in train
        )


def test_probability_evaluation_reports_calibration_and_classification_metrics():
    result = evaluate_probabilistic_predictions(
        [0, 1, 1, 0],
        [0.1, 0.8, 0.7, 0.2],
        calibration_bins=5,
    )
    assert result.sample_count == 4
    assert result.brier_score == pytest.approx(0.045)
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1_score == 1.0
    with pytest.raises(ValueError, match="probabilities"):
        evaluate_probabilistic_predictions([0, 1], [0.1, 1.2])


def test_replay_event_family_ablation_uses_same_cost_assumptions():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        ReplayRow("a", "BTCUSDT", at, "transfer", 1, 100, {300: 0.01}),
        ReplayRow("b", "BTCUSDT", at, "dex_swap", -1, 200, {300: -0.02}),
    ]
    result = summarize_replay_by_event_type(
        rows, horizon_seconds=300, fee_bps_per_side=5
    )
    assert set(result) == {"transfer", "dex_swap"}
    assert result["transfer"].sample_count == 1
    assert result["dex_swap"].sample_count == 1
    assert result["transfer"].total_cost_bps == 10
