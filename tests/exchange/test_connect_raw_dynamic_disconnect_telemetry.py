from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from aitos.exchange.connect_raw_dynamic import connect_raw_dynamic


def _adapter(ws):
    adapter = MagicMock()
    adapter._ws_transport = {
        "state": "idle",
        "connect_attempts": 0,
        "successful_handshakes": 0,
        "frames_received": 0,
        "market_events_received": 0,
        "close_count": 0,
    }
    adapter._get_ws_base_url.return_value = (
        "wss://fstream.binance.com/stream",
        "",
    )
    adapter._ws_connector.return_value.__aenter__.return_value = ws
    return adapter


async def _wait_for_state(adapter, expected):
    async with asyncio.timeout(2):
        while adapter._ws_transport["state"] != expected:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_abnormal_disconnect_captures_close_reason_and_counts_once():
    ws = AsyncMock()
    ws.close_code = 1006
    ws.close_reason = "abnormal closure"
    ws.recv.side_effect = ConnectionResetError("tcp reset")
    adapter = _adapter(ws)
    generator = connect_raw_dynamic(
        adapter,
        get_streams=lambda: ["btcusdt@aggTrade"],
        changed=asyncio.Event(),
        emit_reconnect=False,
        label="trades",
    )

    task = asyncio.create_task(generator.asend(None))
    await _wait_for_state(adapter, "error")
    await asyncio.sleep(0)

    transport = adapter._ws_transport
    assert transport["close_count"] == 1
    assert transport["abnormal_close_count"] == 1
    assert transport["intentional_close_count"] == 0
    assert transport["last_disconnect_class"] == "abnormal_error"
    assert transport["last_close_code"] == 1006
    assert transport["last_close_reason"] == "abnormal closure"
    assert transport["last_close_stream"] == "trades"
    assert transport["last_connection_uptime_seconds"] >= 0
    per_stream = transport["managed_connections"]["trades"]
    assert per_stream["connect_attempts"] == 1
    assert per_stream["successful_handshakes"] == 1
    assert per_stream["abnormal_close_count"] == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await generator.aclose()


@pytest.mark.asyncio
async def test_generator_cancellation_is_not_counted_as_abnormal_disconnect():
    ws = AsyncMock()
    ws.recv.side_effect = asyncio.Event().wait
    adapter = _adapter(ws)
    generator = connect_raw_dynamic(
        adapter,
        get_streams=lambda: ["btcusdt@aggTrade"],
        changed=asyncio.Event(),
        emit_reconnect=False,
        label="trades",
    )

    task = asyncio.create_task(generator.asend(None))
    await _wait_for_state(adapter, "connected")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await generator.aclose()

    transport = adapter._ws_transport
    assert transport["close_count"] == 1
    assert transport["intentional_close_count"] == 1
    assert transport.get("abnormal_close_count", 0) == 0
    assert transport["last_disconnect_class"] == "intentional_cancel"
    assert transport["managed_connections"]["trades"]["intentional_close_count"] == 1
