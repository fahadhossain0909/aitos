import asyncio

import pytest

from aitos.spot.user_stream import SpotUserDataStreamSupervisor


class FakeAdapter:
    def __init__(self):
        self.created = 0
        self.keepalives = 0
        self.closed = []

    async def create_listen_key(self):
        self.created += 1
        return f"listen-{self.created}"

    async def keepalive_listen_key(self, key):
        self.keepalives += 1
        if self.keepalives == 1:
            raise RuntimeError("listen key expired")

    async def close_listen_key(self, key):
        self.closed.append(key)


class FakeWebSocket:
    def __init__(self):
        self.second_key = asyncio.Event()

    async def user_stream(self, key, *, reconnect=True):
        if key == "listen-2":
            self.second_key.set()
        yield {"e": "outboundAccountPosition", "key": key}
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_user_stream_renews_key_and_reconnects_after_keepalive_failure():
    adapter = FakeAdapter()
    websocket = FakeWebSocket()
    received = []
    reconciliations = []
    supervisor = SpotUserDataStreamSupervisor(
        adapter=adapter,
        websocket=websocket,
        on_event=received.append,
        on_connect=lambda: reconciliations.append("reconciled"),
        keepalive_seconds=0.01,
        reconnect_seconds=0.001,
        max_reconnect_seconds=0.01,
    )
    await supervisor.start()
    try:
        await asyncio.wait_for(websocket.second_key.wait(), timeout=1)
        assert adapter.created >= 2
        assert "listen-1" in adapter.closed
        assert any(item["key"] == "listen-2" for item in received)
        assert supervisor.health["restarts"] >= 1
        assert len(reconciliations) >= 2
    finally:
        await supervisor.stop()
    assert not supervisor.health["running"]
