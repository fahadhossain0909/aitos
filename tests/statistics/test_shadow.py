import sqlite3
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import pytest

from aitos.statistics.contracts import FeatureSnapshot, ForecastEnvelope
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
        "forecast-1",
        make_envelope(),
        predicted_up=0.7,
        predicted_variance=0.0001,
        created_at="2026-11-07T10:01:00Z",
    )
    assert ledger.record_outcome(
        "forecast-1",
        actual_return=0.003,
        realised_variance=0.00012,
        resolved_at="2026-11-07T10:05:00Z",
    )
    assert not ledger.record_outcome(
        "forecast-1",
        actual_return=0.003,
        realised_variance=0.00012,
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
        "forecast-1",
        make_envelope(),
        predicted_up=0.7,
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
    assert ledger.record_outcome(
        "forecast-1",
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


def test_shadow_ledger_persists_matching_feature_snapshot(tmp_path):
    ledger = StatisticalShadowLedger(tmp_path / "shadow.sqlite3")
    envelope = make_envelope()
    snapshot = FeatureSnapshot(
        symbol=envelope.symbol,
        horizon=envelope.horizon,
        as_of=envelope.as_of,
        window_start="2026-11-07T09:00:00Z",
        values={"a_stat": 7.5, "order_flow": 6.0},
        source_watermarks={"scanner": envelope.as_of},
        sample_count=envelope.sample_count,
    )
    assert ledger.record_forecast(
        "forecast-with-snapshot",
        envelope,
        predicted_up=0.7,
        predicted_variance=0.0001,
        feature_snapshot=snapshot,
    )
    assert not ledger.record_forecast(
        "forecast-with-snapshot",
        envelope,
        predicted_up=0.7,
        predicted_variance=0.0001,
        feature_snapshot=snapshot,
    )
    with pytest.raises(ValueError, match="does not match"):
        ledger.record_forecast(
            "mismatched-snapshot",
            envelope,
            predicted_up=0.7,
            predicted_variance=0.0001,
            feature_snapshot=FeatureSnapshot(
                symbol="ETHUSDT",
                horizon=envelope.horizon,
                as_of=envelope.as_of,
                window_start="2026-11-07T09:00:00Z",
                values={"a_stat": 7.5},
                sample_count=envelope.sample_count,
            ),
        )


def _record_forecast_worker(args):
    database_path, forecast_id = args
    worker_ledger = StatisticalShadowLedger(database_path, busy_timeout_ms=10000)
    return worker_ledger.record_forecast(
        forecast_id,
        make_envelope(),
        predicted_up=0.7,
        predicted_variance=0.0001,
    )


def test_shadow_ledger_is_idempotent_under_multiprocess_writes(tmp_path):
    database_path = str(tmp_path / "concurrent-shadow.sqlite3")
    ledger = StatisticalShadowLedger(database_path, busy_timeout_ms=10000)
    with ProcessPoolExecutor(
        max_workers=4, mp_context=get_context("spawn")
    ) as executor:
        results = list(
            executor.map(
                _record_forecast_worker,
                [(database_path, "shared-forecast") for _ in range(8)],
            )
        )
    assert sum(results) == 1
    assert ledger.get_forecast("shared-forecast") is not None


def test_shadow_ledger_migrates_legacy_schema(tmp_path):
    database_path = tmp_path / "legacy-shadow.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE statistical_shadow_forecasts (
            forecast_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            horizon TEXT NOT NULL,
            model_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            as_of TEXT NOT NULL,
            feature_snapshot_id TEXT NOT NULL,
            predicted_up REAL NOT NULL,
            predicted_variance REAL NOT NULL,
            expected_return REAL NOT NULL,
            actual_return REAL,
            realised_variance REAL,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    ledger = StatisticalShadowLedger(database_path)
    record = ledger.get_forecast("missing")
    assert record is None
    connection = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(statistical_shadow_forecasts)"
        ).fetchall()
    }
    connection.close()
    assert "side" in columns
    assert "feature_snapshot_json" in columns


def test_shadow_ledger_exposes_pending_records_for_reconciliation(tmp_path):
    ledger = StatisticalShadowLedger(tmp_path / "pending-shadow.sqlite3")
    envelope = make_envelope()
    ledger.record_forecast(
        "pending-forecast",
        envelope,
        predicted_up=0.7,
        predicted_variance=0.0001,
    )
    pending = ledger.unresolved_forecasts(
        symbol=envelope.symbol,
        horizon=envelope.horizon,
        side=envelope.side,
    )
    assert len(pending) == 1
    assert pending[0].forecast_id == "pending-forecast"
    assert pending[0].feature_snapshot is None
    assert ledger.record_outcome(
        "pending-forecast",
        actual_return=0.01,
        realised_variance=0.0001,
    )
    assert ledger.unresolved_forecasts() == ()
