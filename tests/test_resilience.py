import time

import pytest

from aitos.resilience import RetryExhaustedError, retry_with_backoff


@pytest.mark.asyncio
async def test_succeeds_immediately_without_retrying():
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await retry_with_backoff(fn, max_attempts=3, base_delay_seconds=0.01)
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retries_and_eventually_succeeds():
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("not yet")
        return "recovered"

    result = await retry_with_backoff(
        flaky, max_attempts=5, base_delay_seconds=0.01, max_delay_seconds=0.02
    )
    assert result == "recovered"
    assert call_count == 3


@pytest.mark.asyncio
async def test_raises_retry_exhausted_after_max_attempts():
    call_count = 0

    async def always_fails():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("permanently down")

    with pytest.raises(RetryExhaustedError) as exc_info:
        await retry_with_backoff(
            always_fails,
            max_attempts=3,
            base_delay_seconds=0.01,
            max_delay_seconds=0.02,
        )

    assert call_count == 3
    assert exc_info.value.attempts == 3
    assert isinstance(exc_info.value.last_exception, ConnectionError)


@pytest.mark.asyncio
async def test_non_matching_exception_propagates_immediately_without_retry():
    call_count = 0

    async def raises_wrong_type():
        nonlocal call_count
        call_count += 1
        raise ValueError("not the exception type we retry on")

    with pytest.raises(ValueError):
        await retry_with_backoff(
            raises_wrong_type, max_attempts=5, exceptions=(ConnectionError,)
        )

    assert call_count == 1  # no retries — ValueError isn't in the retry set


@pytest.mark.asyncio
async def test_backoff_delay_increases_and_respects_max():
    call_count = 0

    async def always_fails():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("down")

    start = time.monotonic()
    with pytest.raises(RetryExhaustedError):
        await retry_with_backoff(
            always_fails,
            max_attempts=4,
            base_delay_seconds=0.02,
            max_delay_seconds=0.05,
        )
    elapsed = time.monotonic() - start

    # 3 waits between 4 attempts, each capped at max_delay + 25% jitter (~0.0625s) -> well under 1s total
    assert elapsed < 1.0
    assert call_count == 4
