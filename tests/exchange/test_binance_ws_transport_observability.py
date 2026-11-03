from __future__ import annotations

import pytest

from aitos.exchange.binance import BinanceFuturesAdapter


class _FakeWebSocket:
    close_code = 1000
    close_reason = "normal test close"

    def __aiter__(self):
        return self

    async def __anext__(self):
        if getattr(self, "_sent", False):
            raise StopAsyncIteration
        self._sent = True
        return '{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","s":"BTCUSDT"}}'

    async def recv(self):
        return await self.__anext__()


class _FakeWebSocketContext:
    def __init__(self, ws: _FakeWebSocket):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_binance_websocket_transport_records_connection_to_event():
    ws = _FakeWebSocket()
    adapter = BinanceFuturesAdapter(ws_connector=lambda _url: _FakeWebSocketContext(ws))

    stream = adapter._connect_raw(
        "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade",
        None,
        False,
        ["btcusdt@aggTrade"],
    )
    payload, stream_name = await anext(stream)

    assert payload["s"] == "BTCUSDT"
    assert stream_name == "btcusdt@aggTrade"

    telemetry = adapter.websocket_transport_snapshot()
    assert telemetry["state"] == "connected"
    assert telemetry["connect_attempts"] == 1
    assert telemetry["successful_handshakes"] == 1
    assert telemetry["frames_received"] == 1
    assert telemetry["market_events_received"] == 1
    assert telemetry["last_connect_started_at"]
    assert telemetry["last_handshake_at"]
    assert telemetry["last_first_frame_at"]
    assert telemetry["last_market_event_at"]
    assert telemetry["current_url"].startswith(
        "wss://fstream.binance.com/market/stream"
    )

    await stream.aclose()
