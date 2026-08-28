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
