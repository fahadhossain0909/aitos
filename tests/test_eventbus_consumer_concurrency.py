"""Regression coverage for live EventBus consumer lifecycle behavior."""

from __future__ import annotations

import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.eventbus.consumer_concurrency import install_eventbus_consumer_concurrency
from aitos.eventbus.redis_bus import EventBus


@pytest.mark.asyncio
async def test_live_subscription_receives_events_published_after_subscribe(fake_redis):
    """Live-only subscriptions must remain active for subsequent market events."""
    install_eventbus_consumer_concurrency(EventBus)
    bus = EventBus(redis_client=fake_redis)
    await bus.initialize({})
    received = asyncio.Event()
    received_topics: list[str] = []

    async def handler(event: Event) -> None:
        received_topics.append(event.topic)
        received.set()

    topic = "market.trade.PR195TEST"
    try:
        await bus.subscribe(topic, handler, group="pr195-live-subscription", start_id="$")
        # Allow the consumer group and reader task to start before publishing.
        await asyncio.sleep(0.05)
        await bus.publish(
            Event(
                topic=topic,
                payload={"price": "1"},
                source_module="pr195-regression-test",
            )
        )
        await asyncio.wait_for(received.wait(), timeout=2.0)
        assert received_topics == [topic]
    finally:
        await bus.shutdown(grace_period_seconds=2.0)
