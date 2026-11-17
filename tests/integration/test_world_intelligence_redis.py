"""Optional real-Redis integration coverage for WIATS distributed leases."""

import os
import uuid

import pytest
from redis.asyncio import Redis

from aitos.world_intelligence.lease import (
    LeaseUnavailable,
    RedisSourceLeaseManager,
)


@pytest.mark.asyncio
async def test_redis_source_lease_exclusivity_and_fencing_round_trip():
    host = os.getenv("REDIS_HOST")
    if not host:
        pytest.skip("REDIS_HOST is not configured")
    client = Redis(
        host=host,
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=False,
    )
    namespace = f"aitos:wiats:test:{uuid.uuid4().hex}"
    manager = RedisSourceLeaseManager(client, namespace=namespace, ttl_ms=3000)
    try:
        async with manager.hold("integration-source") as first:
            assert first.fencing_token == 1
            with pytest.raises(LeaseUnavailable):
                async with manager.hold("integration-source"):
                    pass
            await first.assert_valid()

        async with manager.hold("integration-source") as second:
            assert second.fencing_token == 2
            await second.assert_valid()
    finally:
        await client.aclose()
