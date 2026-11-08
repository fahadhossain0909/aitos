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
        try:
            await client.ping()
        except BaseException:
            try:
                await client.aclose()
            except Exception as cleanup_error:
                logger.warning(
                    "Redis attempt cleanup failed (%s)", type(cleanup_error).__name__
                )
            raise
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


async def try_connect_neo4j(settings, *, logger):
    """Connect to optional Neo4j storage without blocking core trading startup."""

    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        settings.neo4j.uri,
        auth=(settings.neo4j.user, settings.neo4j.password),
    )
    try:
        await driver.verify_connectivity()
        return driver
    except BaseException as exc:
        try:
            await driver.close()
        except Exception as cleanup_error:
            logger.warning(
                "Neo4j startup cleanup failed (%s)", type(cleanup_error).__name__
            )
        if isinstance(exc, Exception):
            logger.warning("Neo4j unavailable: %s", exc)
            return None
        raise


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
    except BaseException as exc:
        # The caller cannot own these resources until both are returned. Clean
        # up cancellation and unexpected failure too; one failed close must not
        # prevent cleanup of the other repository or hide the startup cause.
        for repository in (journal_repo, market_repo):
            try:
                await repository.shutdown()
            except Exception as cleanup_error:
                logger.warning(
                    "ClickHouse startup cleanup failed (%s)",
                    type(cleanup_error).__name__,
                )
        if isinstance(exc, RetryExhaustedError):
            logger.error("ClickHouse persistence unavailable after retries: %s", exc)
            raise SystemExit(1) from exc
        raise
