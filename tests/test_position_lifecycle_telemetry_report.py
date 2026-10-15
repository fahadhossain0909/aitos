import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "position_lifecycle_telemetry_report.py"
)
spec = importlib.util.spec_from_file_location(
    "position_lifecycle_telemetry_report", SCRIPT
)
reporter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reporter)


def test_report_groups_events_and_detects_stale_monitoring():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    positions = {
        "t1": [
            {
                "event": "position.opened",
                "position_id": "t1",
                "symbol": "BTCUSDT",
                "timestamp": "2026-10-15T11:55:00+00:00",
            },
            {
                "event": "position.evaluation.completed",
                "position_id": "t1",
                "symbol": "BTCUSDT",
                "timestamp": "2026-10-15T11:59:30+00:00",
            },
        ],
        "t2": [
            {
                "event": "position.opened",
                "position_id": "t2",
                "symbol": "ETHUSDT",
                "timestamp": "2026-10-15T11:50:00+00:00",
            },
        ],
    }
    report = reporter.build_report(positions, now=now, stale_after_seconds=120)
    assert report["position_count"] == 2
    assert report["stale_or_unobserved_count"] == 1
    assert report["positions"][0]["monitor_stale"] is False
    assert report["positions"][1]["monitor_stale"] is True


def test_closed_positions_are_not_reported_as_stale():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    positions = {
        "t3": [
            {
                "event": "position.close.completed",
                "position_id": "t3",
                "symbol": "BTCUSDT",
                "state": "position_closed",
                "timestamp": "2026-10-15T10:00:00+00:00",
            }
        ]
    }
    report = reporter.build_report(positions, now=now, stale_after_seconds=120)
    assert report["stale_or_unobserved_count"] == 0
    assert report["positions"][0]["monitor_stale"] is False


def test_load_events_counts_malformed_lines_and_uses_trade_id_fallback():
    positions, malformed = reporter.load_events(
        [
            '{"event":"position.close.failed","trade_id":"t9","symbol":"SOLUSDT"}',
            "not-json",
        ]
    )
    assert malformed == 1
    assert "t9" in positions
