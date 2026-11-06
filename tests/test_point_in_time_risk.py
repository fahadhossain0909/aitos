from datetime import datetime, timedelta, timezone

import pytest

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
    estimator = PointInTimeCorrelationEstimator(min_observations=10, shrinkage_observations=20)
    cutoff = start + timedelta(days=25)
    before = estimator.estimate(rows, as_of=cutoff)
    rows.append({"symbol": "BBB", "timestamp": cutoff + timedelta(days=1), "price": 1_000_000.0})
    after_future_append = estimator.estimate(rows, as_of=cutoff)
    assert before == after_future_append
    assert before[("AAA", "BBB")] == before[("BBB", "AAA")]
    assert 0.0 < before[("AAA", "BBB")] <= 1.0


def test_point_in_time_correlations_fail_closed_without_aligned_history():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"symbol": "AAA", "timestamp": now, "price": 100.0}]
    estimator = PointInTimeCorrelationEstimator(min_observations=3, shrinkage_observations=3)
    assert estimator.estimate(rows, as_of=now) == {}
    with pytest.raises(ValueError, match="timezone-aware"):
        estimator.estimate(
            [{"symbol": "AAA", "timestamp": datetime(2026, 1, 1), "price": 100.0}],
            as_of=now,
        )
