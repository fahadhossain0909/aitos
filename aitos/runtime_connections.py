"""Shared infrastructure connection helpers used by AITOS runtime entrypoints."""

from __future__ import annotations

from redis.asyncio import Redis

from aitos.resilience import RetryExhaustedError, retry_with_backoff


async def connect_redis_with_retry(settings, *, logger) -> Redis:
    """Connect to Redis with the standard retry policy used by paper and live.

    The caller supplies its module logger so operational messages retain the
    correct runtime context. Exhausted retries preserve the existing entrypoint
    contract by logging the failure and exiting with status 1.
    """

    async def _attempt() -> Redis:
        client = Redis.from_url(
            settings.redis.url,
            max_connections=settings.redis.max_connections,
        )
        await client.ping()
        return client

    try:
        return await retry_with_backoff(
            _attempt,
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="Redis connection",
        )
    except RetryExhaustedError as exc:
        logger.error("could not connect to Redis: %s", exc)
        raise SystemExit(1) from exc
