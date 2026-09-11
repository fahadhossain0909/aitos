import asyncio
import time

import pytest

from aitos.exchange.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_acquire_within_capacity_is_immediate():
    limiter = TokenBucketRateLimiter(
        capacity=10, refill_per_second=1, reserved_capacity=0
    )
    start = time.monotonic()
    await limiter.acquire(weight=5)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_acquire_beyond_capacity_waits_for_refill():
    limiter = TokenBucketRateLimiter(
        capacity=2, refill_per_second=20, reserved_capacity=0
    )  # fast refill for test speed
    await limiter.acquire(weight=2)  # drain the bucket
    start = time.monotonic()
    await limiter.acquire(weight=2)  # must wait for refill
    elapsed = time.monotonic() - start
    assert elapsed > 0.05


@pytest.mark.asyncio
async def test_weight_exceeding_capacity_raises():
    limiter = TokenBucketRateLimiter(
        capacity=5, refill_per_second=1, reserved_capacity=0
    )
    with pytest.raises(ValueError):
        await limiter.acquire(weight=10)


@pytest.mark.asyncio
async def test_critical_orderbook_lane_can_use_reserved_capacity():
    limiter = TokenBucketRateLimiter(
        capacity=10,
        refill_per_second=100,
        reserved_capacity=2,
        critical_task_prefixes=("market-data-orderbook",),
    )
    await limiter.acquire(weight=8)

    async def critical_call():
        await limiter.acquire(weight=2)

    task = asyncio.create_task(critical_call(), name="market-data-orderbook")
    await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_normal_call_cannot_consume_reserved_capacity():
    limiter = TokenBucketRateLimiter(
        capacity=10,
        refill_per_second=100,
        reserved_capacity=2,
        critical_task_prefixes=("market-data-orderbook",),
    )
    await limiter.acquire(weight=8)
    start = time.monotonic()
    await limiter.acquire(weight=1)
    elapsed = time.monotonic() - start
    assert elapsed > 0.005


@pytest.mark.asyncio
async def test_wait_telemetry_records_caller_weight_and_duration():
    limiter = TokenBucketRateLimiter(
        capacity=2,
        refill_per_second=20,
        reserved_capacity=0,
    )
    await limiter.acquire(weight=2)

    task = asyncio.create_task(limiter.acquire(weight=2), name="market-data-orderbook")
    await asyncio.wait_for(task, timeout=0.5)

    snapshot = limiter.snapshot()
    assert snapshot["acquire_count"] == 2
    assert snapshot["wait_count"] == 1
    assert snapshot["slow_wait_count"] == 1
    assert snapshot["last_caller"] == "market-data-orderbook"
    assert snapshot["last_weight"] == 2
    assert snapshot["last_critical"] is True
    assert snapshot["last_wait_ms"] >= 50
    assert snapshot["total_wait_ms"] >= 50
    assert snapshot["max_wait_ms"] >= 50
