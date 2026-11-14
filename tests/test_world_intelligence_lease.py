import pytest

from aitos.world_intelligence.lease import (
    LeaseUnavailable,
    RedisSourceLeaseManager,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.counters = {}

    async def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        values = args[numkeys:]
        if "INCR" in script:
            lease_key, fence_key = keys
            owner, ttl = values
            if lease_key in self.values:
                return None
            fence = self.counters.get(fence_key, 0) + 1
            self.counters[fence_key] = fence
            self.values[lease_key] = f"{owner}:{fence}"
            return fence
        key = keys[0]
        owner = values[0]
        if "PEXPIRE" in script:
            return int(self.values.get(key) == owner)
        if "DEL" in script:
            if self.values.get(key) == owner:
                del self.values[key]
                return 1
            return 0
        if "return 1" in script:
            return int(self.values.get(key) == owner)
        raise AssertionError("unexpected Lua script")


@pytest.mark.asyncio
async def test_redis_source_lease_is_exclusive_and_fencing_is_monotonic():
    redis = FakeRedis()
    manager = RedisSourceLeaseManager(redis, ttl_ms=3000)
    async with manager.hold("gdelt") as first:
        assert first.fencing_token == 1
        with pytest.raises(LeaseUnavailable):
            async with manager.hold("gdelt"):
                pass
        await first.assert_valid()
    async with manager.hold("gdelt") as second:
        assert second.fencing_token == 2
        await second.assert_valid()


@pytest.mark.asyncio
async def test_stale_lease_release_cannot_delete_new_owner():
    redis = FakeRedis()
    manager = RedisSourceLeaseManager(redis, ttl_ms=3000)
    lease_context = manager.hold("rss")
    first = await lease_context.__aenter__()
    key = first.key
    redis.values[key] = "new-owner:2"
    await lease_context.__aexit__(None, None, None)
    assert redis.values[key] == "new-owner:2"


def test_lease_manager_validates_ttl_and_source_id():
    with pytest.raises(ValueError, match="ttl_ms"):
        RedisSourceLeaseManager(FakeRedis(), ttl_ms=1000)
    manager = RedisSourceLeaseManager(FakeRedis())
    with pytest.raises(ValueError, match="source_id"):
        manager._keys(" ")
