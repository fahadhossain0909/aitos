from datetime import datetime, timedelta, timezone

from aitos.intelligence.live_scanner import LiveScannerCache


def test_freshness_snapshot_distinguishes_source_age_from_receive_lag():
    cache = LiveScannerCache(event_bus=None, symbols=["BTCUSDT"])
    state = cache._cache("BTCUSDT")
    now = datetime.now(timezone.utc)
    state.last_trade_at = now - timedelta(seconds=2)
    state.last_trade_received_at = now - timedelta(seconds=1.5)
    state.last_book_at = now - timedelta(seconds=8)
    state.last_book_received_at = now - timedelta(seconds=1)

    snapshot = cache.freshness_snapshot("BTCUSDT")

    assert snapshot["cache_has_state"] is True
    assert 1.5 <= snapshot["trade_age_sec"] <= 2.5
    assert 7.5 <= snapshot["book_age_sec"] <= 8.5
    assert 0.0 <= snapshot["trade_consumer_lag_sec"] <= 1.0
    assert 6.0 <= snapshot["book_consumer_lag_sec"] <= 8.0


def test_freshness_snapshot_reports_empty_cache():
    cache = LiveScannerCache(event_bus=None, symbols=["BTCUSDT"])
    snapshot = cache.freshness_snapshot("BTCUSDT")

    assert snapshot["cache_has_state"] is False
    assert snapshot["trade_age_sec"] is None
    assert snapshot["book_age_sec"] is None
