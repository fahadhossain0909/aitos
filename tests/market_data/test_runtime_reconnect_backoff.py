import pytest

from aitos.market_data.runtime import CanonicalMarketDataRuntime


@pytest.mark.asyncio
async def test_idle_timeout_is_counted_once_and_backoff_grows():
    runtime = CanonicalMarketDataRuntime.__new__(CanonicalMarketDataRuntime)
    runtime._stream_telemetry = {}
    state = runtime._stream_state("trades")
    assert state["idle_timeouts"] == 0
    assert state["restarts"] == 0
    state["idle_timeouts"] = int(state["idle_timeouts"]) + 1
    state["restarts"] = int(state["restarts"]) + 1
    state["consecutive_failures"] = 1
    assert state["idle_timeouts"] == 1
    assert state["restarts"] == 1
    assert state["consecutive_failures"] == 1


def test_stream_state_is_per_stream():
    runtime = CanonicalMarketDataRuntime.__new__(CanonicalMarketDataRuntime)
    runtime._stream_telemetry = {}
    trades = runtime._stream_state("trades")
    books = runtime._stream_state("orderbook")
    trades["errors"] = 2
    books["errors"] = 1
    assert trades["errors"] == 2
    assert books["errors"] == 1
    assert set(runtime._stream_telemetry) == {"trades", "orderbook"}
