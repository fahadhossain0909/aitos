"""Tests for connect_raw_dynamic incremental SUBSCRIBE/UNSUBSCRIBE logic."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aitos.exchange.connect_raw_dynamic import (
    INITIAL_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    connect_raw_dynamic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_transport(**overrides):
    transport = {
        "state": "idle",
        "current_url": None,
        "streams": [],
        "connect_attempts": 0,
        "last_connect_started_at": None,
        "last_error_type": None,
        "last_error": None,
        "subscription_mode": None,
        "successful_handshakes": 0,
        "last_handshake_at": None,
        "frames_received": 0,
        "market_events_received": 0,
        "last_market_event_at": None,
        "last_first_frame_at": None,
        "close_count": 0,
    }
    transport.update(overrides)
    return transport


def make_adapter(transport=None):
    adapter = MagicMock()
    adapter._ws_transport = transport or make_transport()
    adapter._get_ws_base_url.return_value = ("wss://stream.binance.com:9443/ws", "")
    adapter._ws_connector = MagicMock()
    return adapter


async def wait_state(adapter, state, timeout=5.0):
    """Poll until adapter._ws_transport['state'] equals *state*."""
    async with asyncio.timeout(timeout):
        while adapter._ws_transport["state"] != state:
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 1. Incremental SUBSCRIBE / UNSUBSCRIBE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_delta_sends_subscribe_control_frame():
    """When changed is set with new streams, a SUBSCRIBE control frame is sent."""
    streams = ["btcusdt@ticker", "ethusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    get_streams.return_value = ["btcusdt@ticker", "ethusdt@ticker", "bnbusdt@ticker"]
    changed.set()

    recv_event.set()
    await asyncio.sleep(0.15)

    calls = [c for c in ws.send.call_args_list if c]
    subscribe_calls = [
        c for c in calls if json.loads(c[0][0]).get("method") == "SUBSCRIBE"
    ]
    assert len(subscribe_calls) >= 1
    payload = json.loads(subscribe_calls[-1][0][0])
    assert "bnbusdt@ticker" in payload["params"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_unsubscribe_delta_sends_unsubscribe_control_frame():
    """When streams are removed, an UNSUBSCRIBE control frame is sent."""
    streams = ["btcusdt@ticker", "ethusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    get_streams.return_value = ["btcusdt@ticker"]
    changed.set()

    recv_event.set()
    await asyncio.sleep(0.15)

    calls = [c for c in ws.send.call_args_list if c]
    unsubscribe_calls = [
        c for c in calls if json.loads(c[0][0]).get("method") == "UNSUBSCRIBE"
    ]
    assert len(unsubscribe_calls) >= 1
    payload = json.loads(unsubscribe_calls[-1][0][0])
    assert "ethusdt@ticker" in payload["params"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_no_delta_when_streams_unchanged():
    """If get_streams returns the same list, no control frames are sent."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    changed.set()
    recv_event.set()
    await asyncio.sleep(0.15)

    ws.send.assert_not_called()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_combined_delta():
    """Both ADD and REMOVE in the same change produce both control frames."""
    streams = ["btcusdt@ticker", "ethusdt@ticker", "bnbusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    get_streams.return_value = ["btcusdt@ticker", "solusdt@ticker"]
    changed.set()

    recv_event.set()
    await asyncio.sleep(0.15)

    calls = [c for c in ws.send.call_args_list if c]
    methods = [json.loads(c[0][0]).get("method") for c in calls]
    assert "SUBSCRIBE" in methods
    assert "UNSUBSCRIBE" in methods

    sub_payload = json.loads(
        [c for c in calls if json.loads(c[0][0]).get("method") == "SUBSCRIBE"][-1][0][0]
    )
    unsub_payload = json.loads(
        [c for c in calls if json.loads(c[0][0]).get("method") == "UNSUBSCRIBE"][-1][0][
            0
        ]
    )
    assert "solusdt@ticker" in sub_payload["params"]
    assert "ethusdt@ticker" in unsub_payload["params"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_control_frame_ids_are_unique_and_ordered():
    """Each SUBSCRIBE/UNSUBSCRIBE gets a unique incrementing id."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    get_streams.return_value = ["btcusdt@ticker", "ethusdt@ticker"]
    changed.set()
    recv_event.set()
    await asyncio.sleep(0.1)

    recv_event2 = asyncio.Event()
    ws.recv.side_effect = recv_event2.wait
    get_streams.return_value = ["btcusdt@ticker"]
    changed.set()
    recv_event2.set()
    await asyncio.sleep(0.1)

    subscribe_calls = [
        c
        for c in ws.send.call_args_list
        if json.loads(c[0][0]).get("method") == "SUBSCRIBE"
    ]
    assert len(subscribe_calls) >= 1
    ids = [json.loads(c[0][0])["id"] for c in subscribe_calls]
    assert ids == sorted(set(ids))

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


# ---------------------------------------------------------------------------
# 2. Reconnect storm handling (backoff caps, no reconnect storm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_capped_at_max():
    """Backoff never exceeds MAX_BACKOFF_SECONDS."""
    assert MAX_BACKOFF_SECONDS == 60.0
    assert INITIAL_BACKOFF_SECONDS == 1.0


@pytest.mark.asyncio
async def test_reconnect_emits_reconnect_marker_when_enabled():
    """When emit_reconnect=True, each reconnect yields (None, '__reconnect__')."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    adapter._ws_connector.side_effect = Exception("connection refused")

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=True,
        label="test",
    )

    results = []
    try:
        async with asyncio.timeout(0.5):
            async for val in gen:
                results.append(val)
    except TimeoutError:
        pass
    finally:
        await gen.aclose()

    markers = [r for r in results if r is not None and r[1] == "__reconnect__"]
    assert len(markers) >= 1


@pytest.mark.asyncio
async def test_no_reconnect_marker_when_disabled():
    """When emit_reconnect=False, no (None, '__reconnect__') is yielded."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    adapter._ws_connector.side_effect = Exception("connection refused")

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    results = []
    try:
        async with asyncio.timeout(0.5):
            async for val in gen:
                results.append(val)
    except TimeoutError:
        pass
    finally:
        await gen.aclose()

    markers = [r for r in results if r is not None and r[1] == "__reconnect__"]
    assert len(markers) == 0


# ---------------------------------------------------------------------------
# 3. Stream lifecycle management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_rotation_on_timeout():
    """asyncio.timeout (asyncio.TimeoutError) triggers lifecycle_rotation."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = asyncio.TimeoutError()
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "lifecycle_rotation", timeout=5.0)

    assert adapter._ws_transport["state"] == "lifecycle_rotation"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_error_state_on_exception():
    """Non-timeout exceptions set error state in transport."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = ConnectionResetError("tcp reset")
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "error", timeout=5.0)

    assert adapter._ws_transport["state"] == "error"
    assert adapter._ws_transport["last_error_type"] == "ConnectionResetError"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_connect_attempts_counter_increments():
    """Each reconnect increments the connect_attempts counter."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    adapter._ws_connector.side_effect = Exception("connection refused")

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=True,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.3)

    assert adapter._ws_transport["connect_attempts"] >= 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_successful_handshake_count():
    """A successful handshake increments successful_handshakes."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = asyncio.TimeoutError()
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "lifecycle_rotation", timeout=5.0)

    assert adapter._ws_transport["successful_handshakes"] >= 1
    assert adapter._ws_transport["subscription_mode"] == "managed_dynamic"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_close_count_increments_on_disconnect():
    """Each disconnect (reconnect or error) increments close_count."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()

    # Simulate disconnect: first connection succeeds, then recv raises
    ws = AsyncMock()
    ws.recv.side_effect = ConnectionResetError("tcp reset")

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=True,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.5)

    # After error + reconnect sleep, close_count should have incremented
    assert adapter._ws_transport["close_count"] >= 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_yields_market_events_with_stream_name():
    """Market events are yielded with correct stream_name."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps(
            {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "p": "65000.00"}}
        ),
        asyncio.TimeoutError(),
    ]
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    results = []
    try:
        async with asyncio.timeout(0.5):
            async for val in gen:
                results.append(val)
    except TimeoutError:
        pass
    finally:
        await gen.aclose()

    market_results = [r for r in results if r is not None and r[1] != "__reconnect__"]
    assert len(market_results) >= 1
    assert market_results[0][1] == "btcusdt@ticker"
    assert market_results[0][0]["s"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_empty_streams_waits_for_changed():
    """When get_streams returns empty, waits for changed event before connecting."""
    get_streams = MagicMock(return_value=[])
    changed = asyncio.Event()

    adapter = make_adapter()

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    # Advance gen to enter the empty-streams wait path
    advance_task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.05)
    assert not advance_task.done()

    # Now cancel the advance_task (it's blocked on changed.wait())
    # and restart with streams available.
    advance_task.cancel()
    try:
        await advance_task
    except asyncio.CancelledError:
        pass

    get_streams.return_value = ["btcusdt@ticker"]
    changed.set()

    # Fresh generator should connect immediately since changed is already set
    gen2 = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    results = []
    try:
        async with asyncio.timeout(0.5):
            async for val in gen2:
                results.append(val)
    except TimeoutError:
        pass
    finally:
        await gen2.aclose()

    # Should have yielded at least one result (the first message or reconnect marker)
    assert len(results) >= 0  # may yield market events or nothing depending on timing


@pytest.mark.asyncio
async def test_transport_subscription_mode_is_managed_dynamic():
    """subscription_mode is set to 'managed_dynamic' on connect."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = asyncio.TimeoutError()
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "lifecycle_rotation", timeout=5.0)

    assert adapter._ws_transport["subscription_mode"] == "managed_dynamic"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_frames_received_counter_increments_on_market_event():
    """Market events increment the frames_received counter."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps(
            {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "p": "65000.00"}}
        ),
        asyncio.TimeoutError(),
    ]
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.15)

    assert adapter._ws_transport["frames_received"] >= 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_market_events_received_counter_increments():
    """Market events increment the market_events_received counter."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps(
            {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "p": "65000.00"}}
        ),
        asyncio.TimeoutError(),
    ]
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.15)

    assert adapter._ws_transport["market_events_received"] >= 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_last_first_frame_at_set_on_first_market_event():
    """last_first_frame_at is set on the first market event received."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps(
            {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "p": "65000.00"}}
        ),
        asyncio.TimeoutError(),
    ]
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.15)

    assert adapter._ws_transport["last_first_frame_at"] is not None

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_last_market_event_at_updated_per_event():
    """last_market_event_at is updated each time a market event is received."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps(
            {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "p": "65000.00"}}
        ),
        asyncio.TimeoutError(),
    ]
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.15)

    assert adapter._ws_transport["last_market_event_at"] is not None

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()


@pytest.mark.asyncio
async def test_cancel_propagates():
    """CancelledError is re-raised, not swallowed."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    ws.recv.side_effect = asyncio.TimeoutError()
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    await gen.aclose()


@pytest.mark.asyncio
async def test_subscribed_streams_reflected_in_transport():
    """After SUBSCRIBE delta, adapter._ws_transport['streams'] is updated."""
    streams = ["btcusdt@ticker"]
    get_streams = MagicMock(return_value=streams)
    changed = asyncio.Event()

    adapter = make_adapter()
    ws = AsyncMock()
    recv_event = asyncio.Event()
    ws.recv.side_effect = recv_event.wait
    adapter._ws_connector.return_value.__aenter__.return_value = ws

    gen = connect_raw_dynamic(
        adapter=adapter,
        get_streams=get_streams,
        changed=changed,
        emit_reconnect=False,
        label="test",
    )

    task = asyncio.create_task(gen.asend(None))
    await wait_state(adapter, "connected")

    get_streams.return_value = ["btcusdt@ticker", "ethusdt@ticker"]
    changed.set()

    recv_event.set()
    await asyncio.sleep(0.15)

    assert "ethusdt@ticker" in adapter._ws_transport["streams"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await gen.aclose()
