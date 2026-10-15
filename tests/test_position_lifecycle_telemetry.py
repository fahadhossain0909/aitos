from types import SimpleNamespace

from aitos.telemetry.position_lifecycle import emit_position_event, position_snapshot


def test_position_snapshot_adds_stable_position_and_trace_correlation():
    trade = SimpleNamespace(
        trade_id="trade-123",
        symbol="BTCUSDT",
        strategy_id="mean-reversion",
        side=SimpleNamespace(value="LONG"),
        state=SimpleNamespace(value="position_opened"),
        entry_price=100.0,
        last_marked_price=101.0,
    )
    snapshot = position_snapshot(trade)
    assert snapshot["position_id"] == "trade-123"
    assert snapshot["trace_id"] == "position:trade-123"
    assert snapshot["side"] == "LONG"
    assert snapshot["state"] == "position_opened"


def test_position_snapshot_handles_missing_optional_fields():
    snapshot = position_snapshot(SimpleNamespace(trade_id="t1", symbol="ETHUSDT"))
    assert snapshot["position_id"] == "t1"
    assert "entry_price" not in snapshot


def test_emit_position_event_never_raises_for_unserializable_values(monkeypatch):
    import aitos.telemetry.position_lifecycle as telemetry

    captured = []
    monkeypatch.setattr(telemetry.logger, "info", lambda *a, **kw: captured.append(kw))
    emit_position_event(
        "position.test",
        SimpleNamespace(trade_id="t1", symbol="ETHUSDT"),
        reasons=("stale_data", "high_risk"),
        opaque=object(),
    )
    assert captured
    payload = captured[0]["extra"]["aitos_extra"]
    assert payload["event"] == "position.test"
    assert payload["position_id"] == "t1"
    assert payload["reasons"] == ["stale_data", "high_risk"]
