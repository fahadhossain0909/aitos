from datetime import datetime, timezone

import pytest

from aitos.statistics.contracts import ForecastEnvelope
from aitos.statistics.shadow import StatisticalShadowLedger


def make_envelope():
    return ForecastEnvelope(
        symbol="BTCUSDT",
        horizon="5m",
        model_id="a-stat",
        model_version="1.0",
        as_of="2026-11-07T09:55:00Z",
        feature_snapshot_id="snapshot-1",
        sample_count=100,
        expected_return=0.002,
        expected_volatility=0.01,
        downside_probability=0.3,
        tail_probability=0.02,
    )


def test_shadow_ledger_persists_and_resolves_forecast_idempotently(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    ledger = StatisticalShadowLedger(path)
    assert ledger.record_forecast(
        "forecast-1",
        make_envelope(),
        predicted_up=0.7,
        predicted_variance=0.0001,
        created_at="2026-11-07T10:00:00Z",
    )
    assert not ledger.record_forecast(
        "forecast-1", make_envelope(), predicted_up=0.7,
        predicted_variance=0.0001,
        created_at="2026-11-07T10:01:00Z",
    )
    assert ledger.record_outcome(
        "forecast-1", actual_return=0.003, realised_variance=0.00012,
        resolved_at="2026-11-07T10:05:00Z",
    )
    assert not ledger.record_outcome(
        "forecast-1", actual_return=0.003, realised_variance=0.00012,
    )
    ledger.close()

    reopened = StatisticalShadowLedger(path)
    metrics = reopened.metrics(symbol="BTCUSDT", horizon="5m")
    assert metrics.observations == 1
    assert metrics.brier == pytest.approx(0.09)
    assert metrics.directional_hit_rate == 1.0
    assert metrics.qlike >= 0.0


def test_shadow_ledger_rejects_conflicting_replay_and_unknown_outcome(tmp_path):
    ledger = StatisticalShadowLedger(tmp_path / "shadow.sqlite3")
    ledger.record_forecast(
        "forecast-1", make_envelope(), predicted_up=0.7,
        predicted_variance=0.0001,
    )
    with pytest.raises(ValueError):
        ledger.record_forecast(
                "forecast-1",
            make_envelope(),
            predicted_up=0.6,
            predicted_variance=0.0001,
        )
    with pytest.raises(KeyError):
        ledger.record_outcome(
            "missing",
            actual_return=0.0,
            realised_variance=0.0,
        )
    with pytest.raises(ValueError):
        ledger.record_outcome(
            "forecast-1",
            actual_return=0.1,
            realised_variance=0.01,
        )


def test_shadow_ledger_rejects_invalid_forecast_probabilities(tmp_path):
    ledger = StatisticalShadowLedger(tmp_path / "shadow.sqlite3")
    with pytest.raises(ValueError):
        ledger.record_forecast(
            "forecast-1",
            make_envelope(),
            predicted_up=1.1,
            predicted_variance=0.0001,
        )
