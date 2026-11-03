from aitos.forensics.end_to_end_telemetry import _key, _stats_add


def test_trace_key_is_stable_for_trade_payload() -> None:
    assert _key({"symbol": "BTCUSDT", "trade_id": 123}) == "BTCUSDT:123"


def test_trace_key_accepts_wire_symbol() -> None:
    assert _key({"s": "ETHUSDT", "trade_id": 456}) == "ETHUSDT:456"


def test_trace_key_ignores_non_trade_payloads() -> None:
    assert _key({"symbol": "BTCUSDT"}) is None
    assert _key({"trade_id": 123}) is None


def test_latency_stats_accumulate_without_losing_samples() -> None:
    stats: dict[str, dict[str, float]] = {}
    _stats_add(stats, "market.trade.BTCUSDT", 2.0)
    _stats_add(stats, "market.trade.BTCUSDT", 6.0)

    assert stats["market.trade.BTCUSDT"]["count"] == 2
    assert stats["market.trade.BTCUSDT"]["total_ms"] == 8.0
    assert stats["market.trade.BTCUSDT"]["max_ms"] == 6.0
