from datetime import datetime, timedelta, timezone

import pytest

from aitos.statistics.contracts import FeatureSnapshot, ForecastEnvelope


def test_feature_snapshot_validates_lineage_and_freshness():
    now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
    snapshot = FeatureSnapshot(
        symbol="BTCUSDT",
        horizon="5m",
        as_of=(now - timedelta(seconds=5)).isoformat(),
        window_start=(now - timedelta(minutes=30)).isoformat(),
        values={"rv": 0.002, "imbalance": -0.2},
        source_watermarks={"trades": (now - timedelta(seconds=6)).isoformat()},
        sample_count=120,
    )
    assert snapshot.is_fresh(10, now=now)
    assert snapshot.to_dict()["symbol"] == "BTCUSDT"


def test_feature_snapshot_rejects_naive_and_future_timestamps():
    with pytest.raises(ValueError):
        FeatureSnapshot(
            "BTCUSDT",
            "1m",
            "2026-11-07T10:00:00",
            "2026-11-07T09:00:00Z",
        )
    future = FeatureSnapshot(
        "BTCUSDT",
        "1m",
        "2026-11-07T10:01:00Z",
        "2026-11-07T09:00:00Z",
    )
    now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
    assert not future.is_fresh(60, now=now)


def test_feature_snapshot_rejects_non_finite_features():
    with pytest.raises(ValueError):
        FeatureSnapshot(
            "BTCUSDT",
            "1m",
            "2026-11-07T10:00:00Z",
            "2026-11-07T09:00:00Z",
            values={"bad": float("nan")},
        )


def test_forecast_envelope_requires_probability_and_identity_contracts():
    result = ForecastEnvelope(
        symbol="BTCUSDT",
        horizon="15m",
        model_id="a-stat",
        model_version="1",
        as_of="2026-11-07T10:00:00Z",
        feature_snapshot_id="snapshot-1",
        sample_count=500,
        expected_return=0.001,
        expected_volatility=0.004,
        downside_probability=0.3,
        tail_probability=0.02,
        expected_shortfall=0.01,
    )
    assert result.is_fresh(
        30, now=datetime(2026, 11, 7, 10, 0, 20, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError):
        ForecastEnvelope(
            symbol="BTCUSDT",
            horizon="15m",
            model_id="a-stat",
            model_version="1",
            as_of="2026-11-07T10:00:00Z",
            feature_snapshot_id="snapshot-1",
            sample_count=20,
            expected_return=0.0,
            expected_volatility=0.1,
            downside_probability=1.2,
            tail_probability=0.1,
        )
