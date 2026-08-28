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

    async def handler(event: Event):
        received.append(event.payload["kind"])

    await event_bus.publish(
        Event(
            topic="market.trade.RESTARTUSDT",
            payload={"kind": "old"},
            source_module="test",
        )
    )

    # Simulate a group left behind by an earlier deployment. A live-only
    # subscriber must not inherit that group's historical cursor.
    await event_bus.subscribe(
        "market.trade.RESTARTUSDT",
        handler,
        group="live-restart-test-v2",
        start_id="0",
    )
    await asyncio.sleep(0.05)

    await event_bus.subscribe(
        "market.trade.RESTARTUSDT",
        handler,
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
