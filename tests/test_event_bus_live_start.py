import asyncio

import pytest

from aitos.core.contracts import Event


@pytest.mark.asyncio
async def test_subscribe_start_id_dollar_skips_existing_stream_backlog(event_bus):
    received = []

    async def handler(event: Event):
        received.append(event.payload["kind"])

    await event_bus.publish(
        Event(
            topic="market.trade.TESTUSDT",
            payload={"kind": "historical"},
            source_module="test",
        )
    )

    await event_bus.subscribe(
        "market.trade.TESTUSDT",
        handler,
        group="live-test-v2",
        start_id="$",
    )

    await event_bus.publish(
        Event(
            topic="market.trade.TESTUSDT",
            payload={"kind": "live"},
            source_module="test",
        )
    )

    for _ in range(30):
        if received:
            break
        await asyncio.sleep(0.1)

    assert received == ["live"]


@pytest.mark.asyncio
async def test_live_start_resets_existing_group_cursor(event_bus):
    received = []

    async def historical_handler(event: Event):
        received.append(event.payload["kind"])

    async def live_handler(event: Event):
        received.append(event.payload["kind"])

    await event_bus.publish(
        Event(
            topic="market.trade.RESTARTUSDT",
            payload={"kind": "old"},
            source_module="test",
        )
    )

    # Simulate a group left behind by an earlier deployment. Stop the old
    # consumer before the replacement subscribes so the test models a real
    # process restart instead of racing two consumers in the same process.
    old_subscription = await event_bus.subscribe(
        "market.trade.RESTARTUSDT",
        historical_handler,
        group="live-restart-test-v2",
        start_id="0",
    )
    await asyncio.sleep(0.05)
    old_subscription.cancel()
    await asyncio.sleep(0)
    received.clear()

    # A live-only replacement must move an existing group's cursor to the
    # current stream tail and must not reclaim abandoned historical entries.
    await event_bus.subscribe(
        "market.trade.RESTARTUSDT",
        live_handler,
        group="live-restart-test-v2",
        start_id="$",
    )

    await event_bus.publish(
        Event(
            topic="market.trade.RESTARTUSDT",
            payload={"kind": "live"},
            source_module="test",
        )
    )

    for _ in range(30):
        if received:
            break
        await asyncio.sleep(0.1)

    assert received == ["live"]
