"""Failed Redis group creation must not poison the local success cache."""

from unittest.mock import AsyncMock

import pytest

from aitos.eventbus.redis_bus import EventBus


async def test_failed_creation_can_be_retried():
    redis = AsyncMock()
    redis.xgroup_create.side_effect = [OSError("transient failure"), None]
    bus = EventBus(redis)
    with pytest.raises(OSError):
        await bus._ensure_group("stream:audit", "audit-group")
    assert ("stream:audit", "audit-group") not in bus._ensured_groups
    await bus._ensure_group("stream:audit", "audit-group")
    assert redis.xgroup_create.await_count == 2
    assert ("stream:audit", "audit-group") in bus._ensured_groups


async def test_failed_live_reset_does_not_publish_success_cache():
    redis = AsyncMock()
    redis.xlen.return_value = 1
    redis.xgroup_create.side_effect = RuntimeError("BUSYGROUP already exists")
    redis.xgroup_setid.side_effect = [OSError("reset failed"), None]
    bus = EventBus(redis)
    with pytest.raises(OSError):
        await bus._ensure_group(
            "stream:audit", "audit-group", start_id="$", reset_existing=True
        )
    assert ("stream:audit", "audit-group") not in bus._ensured_groups
    await bus._ensure_group(
        "stream:audit", "audit-group", start_id="$", reset_existing=True
    )
    assert redis.xgroup_setid.await_count == 2
