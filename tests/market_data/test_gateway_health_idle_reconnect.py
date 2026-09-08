from aitos.market_data.gateway_health import GatewayHealth


def test_idle_timeout_reconnect_is_counted_once():
    health = GatewayHealth("binance", "usd_m_futures")

    health.record_idle_timeout("trades", 30.0)
    health.reconnect()
    health.reconnect()

    assert health.stream_idle_timeouts == 1
    assert health.reconnect_count == 1


def test_normal_reconnect_still_counts_each_attempt():
    health = GatewayHealth("binance", "usd_m_futures")

    health.reconnect()
    health.reconnect()

    assert health.reconnect_count == 2
