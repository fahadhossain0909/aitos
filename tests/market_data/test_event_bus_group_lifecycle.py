from unittest.mock import AsyncMock

import pytest

from aitos.eventbus.redis_bus import EventBus


@pytest.mark.asyncio
async def test_group_creation_is_idempotent_within_bus_lifecycle():
    redis = AsyncMock()
    redis.ping.return_value = True
    bus = EventBus(redis)
    await bus.initialize({})

    await bus._ensure_group("stream:market.trade", "market-scanner")
    await bus._ensure_group("stream:market.trade", "market-scanner")

    assert redis.xgroup_create.await_count == 1


@pytest.mark.asyncio
async def test_live_subscription_can_reset_existing_group_cursor():
    redis = AsyncMock()
    redis.ping.return_value = True
    redis.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
    bus = EventBus(redis)
    await bus.initialize({})

    await bus._ensure_group(
        "stream:market.trade", "market-scanner", start_id="$", reset_existing=True
    )

    redis.xgroup_setid.assert_awaited_once_with(
        "stream:market.trade", "market-scanner", id="$"
    )
