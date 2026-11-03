from types import SimpleNamespace

from aitos.market_data.runtime import CanonicalMarketDataRuntime


def _runtime_with_transport(snapshot: dict[str, object]) -> CanonicalMarketDataRuntime:
    runtime = CanonicalMarketDataRuntime.__new__(CanonicalMarketDataRuntime)
    runtime.adapter = SimpleNamespace(
        exchange=SimpleNamespace(websocket_transport_snapshot=lambda: snapshot)
    )
    return runtime


def test_orderbook_timeout_is_bootstrap_timeout_when_handshake_has_no_first_frame():
    runtime = _runtime_with_transport(
        {
            "state": "connected",
            "last_handshake_at": "2026-09-11T03:08:25.913+00:00",
            "last_first_frame_at": None,
        }
    )

    kind, snapshot = runtime._classify_timeout("orderbook")

    assert kind == "bootstrap_ready_timeout"
    assert snapshot["last_first_frame_at"] is None


def test_orderbook_timeout_is_idle_timeout_after_first_frame():
    runtime = _runtime_with_transport(
        {
            "state": "connected",
            "last_handshake_at": "2026-09-11T03:08:25.913+00:00",
            "last_first_frame_at": "2026-09-11T03:08:26.100+00:00",
        }
    )

    kind, _ = runtime._classify_timeout("orderbook")

    assert kind == "idle_timeout"


def test_non_orderbook_timeout_remains_idle_timeout():
    runtime = _runtime_with_transport(
        {
            "state": "connected",
            "last_handshake_at": None,
            "last_first_frame_at": None,
        }
    )

    kind, _ = runtime._classify_timeout("trades")

    assert kind == "idle_timeout"
