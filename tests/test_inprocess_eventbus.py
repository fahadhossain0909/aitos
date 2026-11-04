"""Behavioral tests for the volatile in-process EventBus transport."""

import asyncio
from datetime import datetime, timezone

from aitos.core.contracts import Event, EventResponse, ModuleStatus
from aitos.eventbus.contracts import EventBusProtocol
from aitos.eventbus.inprocess_bus import InProcessBus


async def _new_bus() -> InProcessBus:
    bus = InProcessBus(history_limit=8)
    await bus.initialize({})
    return bus


def test_inprocess_bus_satisfies_transport_protocol() -> None:
    bus = InProcessBus()
    assert isinstance(bus, EventBusProtocol)


async def test_publish_delivers_to_matching_subscription() -> None:
    bus = await _new_bus()
    received: list[Event] = []
    ready = asyncio.Event()

    async def handler(event: Event) -> None:
        received.append(event)
        ready.set()

    sub = await bus.subscribe("market.*", handler, start_id="$")
    await bus.publish(Event(topic="market.trade", payload={"price": 10}))
    await asyncio.wait_for(ready.wait(), timeout=1)
    assert [event.topic for event in received] == ["market.trade"]
    sub.cancel()
    await bus.shutdown()


async def test_start_zero_replays_bounded_in_memory_history() -> None:
    bus = await _new_bus()
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    await bus.publish(Event(topic="market.trade", payload={"n": 2}))
    received: list[int] = []
    ready = asyncio.Event()

    async def handler(event: Event) -> None:
        received.append(event.payload["n"])
        if len(received) == 2:
            ready.set()

    await bus.subscribe("market.trade", handler, start_id="0")
    await asyncio.wait_for(ready.wait(), timeout=1)
    assert received == [1, 2]
    await bus.shutdown()


async def test_request_reply_returns_handler_response() -> None:
    bus = await _new_bus()

    async def handler(event: Event) -> EventResponse:
        return EventResponse(
            request_event_id=event.event_id,
            responder_module="test-handler",
            payload={"accepted": True},
        )

    await bus.subscribe("risk.check", handler, start_id="$")
    response = await bus.request_reply(
        Event(topic="risk.check", payload={"symbol": "BTCUSDT"}),
        timeout_ms=1000,
    )
    assert response.success is True
    assert response.responder_module == "test-handler"
    assert response.payload == {"accepted": True}
    await bus.shutdown()


async def test_health_marks_transport_as_volatile() -> None:
    bus = await _new_bus()
    health = await bus.health_check()
    assert health.status is ModuleStatus.HEALTHY
    assert health.details["durable"] is False
    await bus.shutdown()


async def test_replay_filters_by_event_timestamp() -> None:
    bus = await _new_bus()
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    result: list[Event] = []
    await bus.replay(
        "market.trade",
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        lambda event: _append(result, event),
    )
    assert len(result) == 1
    await bus.shutdown()


async def _append(result: list[Event], event: Event) -> None:
    result.append(event)


async def test_history_limit_is_global_across_topics() -> None:
    bus = InProcessBus(history_limit=2)
    await bus.initialize({})
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    await bus.publish(Event(topic="market.book", payload={"n": 2}))
    await bus.publish(Event(topic="market.trade", payload={"n": 3}))

    received: list[Event] = []
    await bus.replay(
        "market.trade",
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        lambda event: _append(received, event),
    )
    assert [event.payload["n"] for event in received] == [3]
    health = await bus.health_check()
    assert health.details["history_limit_total"] == 2
    await bus.shutdown()


async def test_subscription_cancel_stops_future_delivery() -> None:
    bus = await _new_bus()
    received: list[Event] = []
    first_event = asyncio.Event()

    async def handler(event: Event) -> None:
        received.append(event)
        first_event.set()

    subscription = await bus.subscribe("market.*", handler, start_id="$")
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    await asyncio.wait_for(first_event.wait(), timeout=1)
    subscription.cancel()
    await asyncio.sleep(0)
    await bus.publish(Event(topic="market.trade", payload={"n": 2}))
    await asyncio.sleep(0)
    assert [event.payload["n"] for event in received] == [1]
    await bus.shutdown()


async def test_handler_failure_is_counted_and_consumer_continues() -> None:
    bus = await _new_bus()
    received: list[int] = []
    second_event = asyncio.Event()

    async def handler(event: Event) -> None:
        if event.payload["n"] == 1:
            raise RuntimeError("expected test failure")
        received.append(event.payload["n"])
        second_event.set()

    await bus.subscribe("market.trade", handler, start_id="$")
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    await bus.publish(Event(topic="market.trade", payload={"n": 2}))
    await asyncio.wait_for(second_event.wait(), timeout=1)
    health = await bus.health_check()
    assert received == [2]
    assert health.details["handler_failures"] == 1
    await bus.shutdown()


async def test_request_reply_timeout_cleans_pending_future() -> None:
    bus = await _new_bus()
    try:
        try:
            await bus.request_reply(
                Event(topic="risk.check", payload={}),
                timeout_ms=1,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("request_reply should time out without a subscriber")
        assert bus._pending_replies == {}
    finally:
        await bus.shutdown()


async def test_shutdown_drains_queued_events_within_grace_period() -> None:
    bus = await _new_bus()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    received: list[int] = []

    async def handler(event: Event) -> None:
        handler_started.set()
        await release_handler.wait()
        received.append(event.payload["n"])

    await bus.subscribe("market.trade", handler, start_id="$")
    await bus.publish(Event(topic="market.trade", payload={"n": 1}))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    shutdown_task = asyncio.create_task(bus.shutdown(grace_period_seconds=1))
    await asyncio.sleep(0)
    release_handler.set()
    await asyncio.wait_for(shutdown_task, timeout=1)
    assert received == [1]
