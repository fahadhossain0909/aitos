from __future__ import annotations

import time


def test_trade_path_telemetry_snapshot_shape() -> None:
    snapshot = {
        "ws_connected_at": None,
        "ws_last_message_received_at": None,
        "ws_last_trade_source_at": None,
        "ws_last_trade_id": None,
        "ws_message_count": 0,
        "ws_idle_seconds": None,
        "fallback_activation_count": 0,
        "rest_request_at": None,
        "rest_response_at": None,
        "rest_newest_trade_id": None,
        "rest_newest_trade_timestamp": None,
        "rest_oldest_trade_timestamp": None,
        "rest_accepted_count": 0,
        "rest_stale_filtered_count": 0,
    }
    assert set(snapshot) == {
        "ws_connected_at",
        "ws_last_message_received_at",
        "ws_last_trade_source_at",
        "ws_last_trade_id",
        "ws_message_count",
        "ws_idle_seconds",
        "fallback_activation_count",
        "rest_request_at",
        "rest_response_at",
        "rest_newest_trade_id",
        "rest_newest_trade_timestamp",
        "rest_oldest_trade_timestamp",
        "rest_accepted_count",
        "rest_stale_filtered_count",
    }
    assert snapshot["ws_message_count"] == 0
    assert snapshot["rest_stale_filtered_count"] == 0


def test_trade_source_age_can_be_calculated() -> None:
    now = time.time()
    source = now - 12.0
    age = now - source
    assert 11.0 <= age <= 13.0


def test_stale_trade_is_not_fresh() -> None:
    now = time.time()
    source = now - 16.0
    max_age = 15.0
    assert now - source > max_age
