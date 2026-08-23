import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from aitos.core.contracts import Event, EventResponse
from aitos.core.exceptions import ModuleNotInitializedError


@pytest.mark.asyncio
async def test_publish_and_subscribe_delivers_event(event_bus):
    received = []

    async def handler(event: Event):
        received.append(event)
        return None

    await event_bus.subscribe("orders.filled", handler, group="test-group")
    await event_bus.publish(
        Event(
            topic="orders.filled", payload={"symbol": "ETHUSDT"}, source_module="test"
        )
    )

    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].topic == "orders.filled"
    assert received[0].payload["symbol"] == "ETHUSDT"


@pytest.mark.asyncio
async def test_request_reply_round_trip(event_bus):
    async def handler(event: Event):
        return EventResponse(
            request_event_id=event.event_id,
            responder_module="echo-agent",
            payload={"echo": event.payload},
        )

    await event_bus.subscribe("echo.request", handler, group="echo-group")

    response = await event_bus.request_reply(
        Event(topic="echo.request", payload={"ping": True}, source_module="test"),
        timeout_ms=3000,
    )

    assert response.success is True
    assert response.payload["echo"]["ping"] is True


@pytest.mark.asyncio
async def test_replay_returns_historical_events(event_bus):
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    await event_bus.publish(
        Event(topic="journal.entry", payload={"trade_id": "t1"}, source_module="test")
    )
    await event_bus.publish(
        Event(topic="journal.entry", payload={"trade_id": "t2"}, source_module="test")
    )

    replayed = []

    async def handler(event: Event):
        replayed.append(event.payload["trade_id"])

    await event_bus.replay("journal.entry", since=since, handler=handler)
    assert replayed == ["t1", "t2"]


@pytest.mark.asyncio
async def test_publish_preserves_event_priority_by_default(event_bus):
    """Regression test: publish() must not silently downgrade an event's own
    priority to NORMAL when no explicit override is passed."""
    from aitos.core.contracts import EventPriority

    received = []

    async def handler(event: Event):
        received.append(event)

    await event_bus.subscribe("alerts.critical", handler, group="test")
    await event_bus.publish(
        Event(
            topic="alerts.critical",
            payload={},
            source_module="test",
            priority=EventPriority.CRITICAL,
        )
    )

    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.1)

    assert received[0].priority == EventPriority.CRITICAL


@pytest.mark.asyncio
async def test_publish_before_initialize_raises(fake_redis):
    from aitos.eventbus.redis_bus import EventBus

    uninitialized_bus = EventBus(redis_client=fake_redis)
    with pytest.raises(ModuleNotInitializedError):
        await uninitialized_bus.publish(Event(topic="x", payload={}))
