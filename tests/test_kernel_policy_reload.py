import asyncio

import fakeredis.aioredis

from aitos.eventbus.redis_bus import EventBus
from aitos.journal.policy_registry import PolicyRegistry
from aitos.kernel.ai_kernel import AIKernel


def test_kernel_loads_persisted_policy_on_initialize(tmp_path):
    async def run():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
        bus = EventBus(redis_client=redis)
        await bus.initialize({})
        try:
            registry = PolicyRegistry(str(tmp_path / "active.json"), {"a": 1.0})
            registry.activate("v2", {"a": 1.0}, 0.7)
            kernel = AIKernel(bus, policy_registry=registry)
            await kernel.initialize({})
            assert kernel.policy_version == "v2"
            assert kernel.fusion_min_confidence == 0.7
            assert kernel.fusion_weights == {"a": 1.0}
            await kernel.shutdown(grace_period_seconds=1.0)
        finally:
            await bus.shutdown(grace_period_seconds=1.0)
            await redis.aclose()

    asyncio.run(run())
