import asyncio
import time

import pytest

from aitos.exchange.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_acquire_within_capacity_is_immediate():
    limiter = TokenBucketRateLimiter(capacity=10, refill_per_second=1)
    start = time.monotonic()
    await limiter.acquire(weight=5)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_acquire_beyond_capacity_waits_for_refill():
    limiter = TokenBucketRateLimiter(
        capacity=2, refill_per_second=20
    )  # fast refill for test speed
    await limiter.acquire(weight=2)  # drain the bucket
    start = time.monotonic()
    await limiter.acquire(weight=2)  # must wait for refill
    elapsed = time.monotonic() - start
    assert elapsed > 0.05


@pytest.mark.asyncio
async def test_weight_exceeding_capacity_raises():
    limiter = TokenBucketRateLimiter(capacity=5, refill_per_second=1)
    with pytest.raises(ValueError):
        await limiter.acquire(weight=10)
