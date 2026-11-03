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
async def test_live_start_resets_existing_group_cursor_without_reclaiming_pending(
    event_bus,
):
    received = []
    topic = "market.trade.RESTARTUSDT"
    group = "live-restart-test-v3"
    stream = f"stream:{topic}"

    async def handler(event: Event):
        received.append(event.payload["kind"])

    await event_bus.publish(
        Event(
            topic=topic,
            payload={"kind": "old"},
            source_module="test",
        )
    )

    # Build a realistic abandoned PEL entry without allowing the test handler
    # to process it. This represents a previous live process that received an
    # event and then died before ACKing it.
    await event_bus._redis.xgroup_create(stream, group, id="0", mkstream=True)
    pending = await event_bus._redis.xreadgroup(
        groupname=group,
        consumername="previous-live-process",
        streams={stream: ">"},
        count=1,
        block=1,
    )
    assert pending

    await event_bus.subscribe(
        topic,
        handler,
        group=group,
        start_id="$",
    )

    await event_bus.publish(
        Event(
            topic=topic,
            payload={"kind": "live"},
            source_module="test",
        )
    )

    for _ in range(30):
        if received:
            break
        await asyncio.sleep(0.1)

    # Live trading must see only the new event. The abandoned historical PEL
    # entry remains out of the live path; durable consumers use start_id="0"
    # and pending recovery instead.
    assert received == ["live"]
