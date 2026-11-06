from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_objective import OpportunityEstimate
from aitos.intelligence.point_in_time_risk import PointInTimeCorrelationEstimator


def test_point_in_time_correlations_are_symmetric_and_exclude_future_prices():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price_a = price_b = 100.0
    for index in range(31):
        timestamp = start + timedelta(days=index)
        move = 0.01 if index % 2 else -0.01
        price_a *= 1.0 + move
        price_b *= 1.0 + move
        for symbol, price in (("AAA", price_a), ("BBB", price_b)):
            rows.append({"symbol": symbol, "timestamp": timestamp, "price": price})
    estimator = PointInTimeCorrelationEstimator(
        min_observations=10, shrinkage_observations=20
    )
    cutoff = start + timedelta(days=25)
    before = estimator.estimate(rows, as_of=cutoff)
    rows.append(
        {"symbol": "BBB", "timestamp": cutoff + timedelta(days=1), "price": 1_000_000.0}
    )
    after_future_append = estimator.estimate(rows, as_of=cutoff)
    assert before == after_future_append
    assert before[("AAA", "BBB")] == before[("BBB", "AAA")]
    assert 0.0 < before[("AAA", "BBB")] <= 1.0


def test_point_in_time_correlations_fail_closed_without_aligned_history():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"symbol": "AAA", "timestamp": now, "price": 100.0}]
    estimator = PointInTimeCorrelationEstimator(
        min_observations=3, shrinkage_observations=3
    )
    assert estimator.estimate(rows, as_of=now) == {}
    with pytest.raises(ValueError, match="timezone-aware"):
        estimator.estimate(
            [{"symbol": "AAA", "timestamp": datetime(2026, 1, 1), "price": 100.0}],
            as_of=now,
        )


def test_capital_gateway_uses_point_in_time_return_history():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    left = right = 100.0
    for index in range(31):
        stamp = start + timedelta(days=index)
        move = 0.01 if index % 2 else -0.01
        left *= 1.0 + move
        right *= 1.0 + move
        rows.extend(
            [
                {"symbol": "AAA", "timestamp": stamp, "price": left},
                {"symbol": "BBB", "timestamp": stamp, "price": right},
            ]
        )
    estimate = OpportunityEstimate(
        symbol="CCC",
        expected_gross_return_pct=1.0,
        expected_loss_pct=1.0,
        loss_probability=0.1,
        fee_bps=2.0,
        slippage_bps=2.0,
        metadata={
            "decision_at": start + timedelta(days=30),
            "return_observations": rows,
        },
    )
    snapshot = CapitalGateway._snapshot(10_000.0, estimate)
    assert snapshot.correlation("AAA", "BBB", 0.75) > 0.0
    assert snapshot.correlation("BBB", "AAA", 0.75) == snapshot.correlation(
        "AAA", "BBB", 0.75
    )


def test_correlation_uses_identical_return_intervals_when_one_asset_has_gaps():
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = []
    left = right = 100.0
    for index in range(35):
        stamp = start + timedelta(days=index)
        move = 0.01 if index % 2 else -0.01
        left *= 1.0 + move
        right *= 1.0 + move
        rows.append({"symbol": "AAA", "timestamp": stamp, "price": left})
        if index not in {8, 17, 26}:
            rows.append({"symbol": "BBB", "timestamp": stamp, "price": right})
    estimator = PointInTimeCorrelationEstimator(
        min_observations=10, shrinkage_observations=20
    )
    result = estimator.estimate(rows, as_of=start + timedelta(days=34))
    assert result[("AAA", "BBB")] == pytest.approx(1.0)


def test_point_in_time_covariance_is_symmetric_and_bounded_by_cutoff():
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rows = []
    left = right = 100.0
    for index in range(30):
        stamp = start + timedelta(days=index)
        move = 0.005 if index % 2 else -0.005
        left *= 1.0 + move
        right *= 1.0 + move
        rows.extend(
            [
                {"symbol": "AAA", "timestamp": stamp, "price": left},
                {"symbol": "BBB", "timestamp": stamp, "price": right},
            ]
        )
    estimator = PointInTimeCorrelationEstimator(
        min_observations=10, shrinkage_observations=20
    )
    covariance = estimator.estimate_covariance(rows, as_of=start + timedelta(days=29))
    assert covariance[("AAA", "BBB")] == covariance[("BBB", "AAA")]
    assert covariance[("AAA", "AAA")] > 0.0
    assert covariance[("AAA", "BBB")] > 0.0
