"""Opt-in real Redis tests with exclusively test-owned Unix-socket storage.

Set AITOS_TEST_REDIS_SERVER to a locally installed redis-server binary. No remote
Redis URL is accepted; these tests never connect to an existing database.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from aitos.core.contracts import Event
from aitos.eventbus import redis_bus
from aitos.eventbus.redis_bus import EventBus

pytestmark = pytest.mark.integration


class IsolatedRedis:
    def __init__(self, binary, directory):
        self.binary = binary
        self.directory = directory
        self.socket = directory / "redis.sock"
        self.process = None
        self.client = None

    async def start(self):
        self.process = subprocess.Popen(
            [
                self.binary,
                "--port",
                "0",
                "--unixsocket",
                str(self.socket),
                "--unixsocketperm",
                "700",
                "--dir",
                str(self.directory),
                "--save",
                "",
                "--appendonly",
                "yes",
                "--appendfsync",
                "always",
                "--logfile",
                str(self.directory / "redis.log"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = Redis(unix_socket_path=str(self.socket))
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError((self.directory / "redis.log").read_text())
            try:
                await self.client.ping()
                return
            except (RedisConnectionError, OSError):
                pass
            await asyncio.sleep(0.01)
        raise TimeoutError("test Redis did not start")

    async def stop(self, *, crash=False):
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self.process is not None:
            if self.process.poll() is None:
                if crash:
                    self.process.kill()
                else:
                    self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)
            self.process = None


@pytest_asyncio.fixture
async def isolated_redis(tmp_path):
    configured = os.getenv("AITOS_TEST_REDIS_SERVER")
    if not configured:
        pytest.skip(
            "requires explicit AITOS_TEST_REDIS_SERVER; no external service fallback"
        )
    binary = shutil.which(configured) or str(Path(configured).resolve())
    if not Path(binary).is_file():
        pytest.fail("AITOS_TEST_REDIS_SERVER is not an executable redis-server path")
    service = IsolatedRedis(binary, tmp_path)
    try:
        await service.start()
        yield service
    finally:
        await service.stop()


async def wait_until(predicate):
    for _ in range(500):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected Redis delivery/ACK state was not reached")


async def no_pending(client, stream, group):
    return (await client.xpending(stream, group))["pending"] == 0


async def test_real_stream_delivery_and_acknowledgement(isolated_redis):
    client = isolated_redis.client
    bus = EventBus(client)
    received = []

    async def handler(event):
        received.append(event)

    await bus.initialize({})
    try:
        await bus.subscribe("audit.roundtrip", handler, group="audit")
        original = Event(topic="audit.roundtrip", payload={"sequence": 1})
        await bus.publish(original)

        async def complete():
            return len(received) == 1 and await no_pending(
                client, "stream:audit.roundtrip", "audit"
            )

        await wait_until(complete)
        assert received[0].event_id == original.event_id
        assert received[0].payload == original.payload
        assert bus._acked_events == 1
    finally:
        await bus.shutdown()


async def test_pending_event_survives_redis_crash_and_is_reclaimed(
    isolated_redis, monkeypatch
):
    client = isolated_redis.client
    original = Event(topic="audit.recover", payload={"sequence": 2})
    await client.xgroup_create("stream:audit.recover", "durable", id="0", mkstream=True)
    await client.xadd("stream:audit.recover", original.to_wire())
    await client.xreadgroup(
        "durable", "dead-consumer", {"stream:audit.recover": ">"}, count=1
    )
    assert (await client.xpending("stream:audit.recover", "durable"))["pending"] == 1
    # appendfsync=always: acknowledged source event + PEL are on the test AOF.
    await isolated_redis.stop(crash=True)
    await isolated_redis.start()
    client = isolated_redis.client
    assert (await client.xpending("stream:audit.recover", "durable"))["pending"] == 1
    monkeypatch.setattr(redis_bus, "PENDING_RECLAIM_IDLE_MS", 0)
    received = []

    async def handler(event):
        received.append(event.event_id)

    bus = EventBus(client)
    await bus.initialize({})
    try:
        await bus.subscribe("audit.recover", handler, group="durable")

        async def complete():
            return received == [original.event_id] and await no_pending(
                client, "stream:audit.recover", "durable"
            )

        await wait_until(complete)
        assert bus._acked_events == 1
    finally:
        await bus.shutdown()


async def test_failed_handler_retries_then_acknowledges(isolated_redis):
    client = isolated_redis.client
    bus = EventBus(client)
    calls = []

    async def handler(event):
        calls.append(event.event_id)
        if len(calls) == 1:
            raise RuntimeError("injected handler failure")

    await bus.initialize({})
    try:
        await bus.subscribe("audit.retry", handler, group="retry")
        original = Event(topic="audit.retry", payload={})
        await bus.publish(original)

        async def complete():
            return len(calls) == 2 and await no_pending(
                client, "stream:audit.retry", "retry"
            )

        await wait_until(complete)
        assert calls == [original.event_id, original.event_id]
        assert bus._retry_events == 1
        assert bus._dlq_events == 0
    finally:
        await bus.shutdown()


async def test_poison_event_reaches_dlq_without_pending_loss(isolated_redis):
    client = isolated_redis.client
    bus = EventBus(client)

    async def handler(event):
        raise RuntimeError("injected poison event")

    await bus.initialize({})
    try:
        await bus.subscribe("audit.poison", handler, group="poison")
        original = Event(topic="audit.poison", payload={"reason": "test-only"})
        await bus.publish(original)

        async def complete():
            return bus._dlq_events == 1 and await no_pending(
                client, "stream:audit.poison", "poison"
            )

        await wait_until(complete)
        entries = await client.xrange(redis_bus.DLQ_STREAM)
        assert len(entries) == 1
        assert Event.from_wire(entries[0][1]).event_id == original.event_id
        assert entries[0][1][b"consumer_group"] == b"poison"
    finally:
        await bus.shutdown()


async def test_retry_write_failure_leaves_real_pending_entry_unacknowledged(
    isolated_redis, monkeypatch
):
    from unittest.mock import AsyncMock

    client = isolated_redis.client
    stream, group = "stream:audit.write-failure", "write-failure"
    event = Event(topic="audit.write-failure", payload={})
    await client.xgroup_create(stream, group, id="0", mkstream=True)
    entry_id = await client.xadd(stream, event.to_wire())
    await client.xreadgroup(group, "failed-consumer", {stream: ">"}, count=1)
    fields = (await client.xrange(stream))[0][1]
    original_xadd = client.xadd
    monkeypatch.setattr(
        client, "xadd", AsyncMock(side_effect=OSError("injected retry write failure"))
    )
    bus = EventBus(client)
    with pytest.raises(OSError, match="retry write failure"):
        await bus._handle_failed_event(
            stream, group, entry_id, fields, RuntimeError("handler")
        )
    assert (await client.xpending(stream, group))["pending"] == 1
    assert await client.xlen(stream) == 1
    monkeypatch.setattr(client, "xadd", original_xadd)
    await bus._handle_failed_event(
        stream, group, entry_id, fields, RuntimeError("handler")
    )
    assert (await client.xpending(stream, group))["pending"] == 0
    assert await client.xlen(stream) == 2


async def test_live_tail_does_not_reclaim_historical_pending_entries(isolated_redis):
    client = isolated_redis.client
    stream, group = "stream:audit.live", "live"
    historical = Event(topic="audit.live", payload={"age": "historical"})
    await client.xgroup_create(stream, group, id="0", mkstream=True)
    await client.xadd(stream, historical.to_wire())
    await client.xreadgroup(group, "old-consumer", {stream: ">"}, count=1)
    received = []

    async def handler(event):
        received.append(event.event_id)

    bus = EventBus(client)
    await bus.initialize({})
    try:
        await bus.subscribe("audit.live", handler, group=group, start_id="$")
        fresh = Event(topic="audit.live", payload={"age": "fresh"})
        await bus.publish(fresh)

        async def complete():
            return received == [fresh.event_id] and bus._acked_events == 1

        await wait_until(complete)
        assert (await client.xpending(stream, group))["pending"] == 1
    finally:
        await bus.shutdown()
