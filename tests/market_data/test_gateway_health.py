from aitos.market_data.gateway_health import GatewayHealth


def test_health_tracks_event_age_and_failures() -> None:
    health = GatewayHealth("binance", "usd_m_futures")
    health.record_event()
    health.record_publish()
    health.record_error("decode", "bad payload")
    snapshot = health.snapshot()
    assert snapshot["received_events"] == 1
    assert snapshot["published_events"] == 1
    assert snapshot["decode_errors"] == 1
    assert snapshot["degraded"] is True
    assert snapshot["source_age_ms"] is not None
