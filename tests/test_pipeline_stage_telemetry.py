import asyncio

from aitos.forensics.pipeline_stage_telemetry import (
    _WSReceiveProxy,
    _cgroup_stats,
    _trace_id,
)


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = iter(messages)

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Adapter:
    pass


class _NoopLogger:
    def debug(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def test_trace_id_uses_raw_trade_id_namespace():
    envelope = {
        "stream": "btcusdt@aggTrade",
        "data": {"s": "BTCUSDT", "a": 900, "l": 1234, "T": 1000},
    }
    assert _trace_id(envelope, envelope["stream"]) == "md:BTCUSDT:1234"


def test_ws_receive_timestamp_is_after_message_arrival(monkeypatch):
    adapter = _Adapter()
    proxy = _WSReceiveProxy(
        _FakeWebSocket(
            ['{"stream":"btcusdt@aggTrade","data":{"s":"BTCUSDT","l":123,"T":1000}}']
        ),
        adapter,
    )

    # The receive timestamp is sampled exactly once by the proxy. Stub the
    # logger separately because logging itself may consult wall-clock time.
    monkeypatch.setattr(
        "aitos.forensics.pipeline_stage_telemetry._logger",
        lambda: _NoopLogger(),
    )
    monkeypatch.setattr(
        "aitos.forensics.pipeline_stage_telemetry.time.time",
        lambda: 10.0,
    )
    perf_clock = iter([1.0, 1.1])
    monkeypatch.setattr(
        "aitos.forensics.pipeline_stage_telemetry.time.perf_counter",
        lambda: next(perf_clock),
    )

    message = asyncio.run(proxy.__anext__())

    assert "BTCUSDT" in message
    assert adapter._e2e_ws_stats["messages"] == 1
    assert adapter._e2e_ws_recent[0]["received_at_ms"] == 10000.0
    assert adapter._e2e_ws_recent[0]["source_age_ms"] == 9000.0
    assert adapter._e2e_ws_recent[0]["receive_wait_ms"] == 100.0


def test_cgroup_stats_is_safe_when_procfs_is_unavailable(monkeypatch):
    def missing_open(*args, **kwargs):
        raise OSError("missing cgroup")

    monkeypatch.setattr("builtins.open", missing_open)
    assert _cgroup_stats() is None
