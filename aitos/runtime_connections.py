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


async def connect_clickhouse_repositories(settings, *, logger):
    """Initialize the shared market-data and journal repositories with retries."""

    from aitos.data.repository import MarketDataRepository
    from aitos.journal.repository import JournalRepository

    market_repo = MarketDataRepository(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    journal_repo = JournalRepository(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    try:
        await retry_with_backoff(
            lambda: market_repo.initialize({}),
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="ClickHouse market repository initialization",
        )
        await retry_with_backoff(
            lambda: journal_repo.initialize({}),
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="ClickHouse journal repository initialization",
        )
        return market_repo, journal_repo
    except RetryExhaustedError as exc:
        await market_repo.shutdown()
        await journal_repo.shutdown()
        logger.error("ClickHouse persistence unavailable after retries: %s", exc)
        raise SystemExit(1) from exc
