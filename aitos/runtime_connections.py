"""Shared infrastructure connection helpers used by AITOS runtime entrypoints."""

from __future__ import annotations

from redis.asyncio import Redis

from aitos.resilience import RetryExhaustedError, retry_with_backoff


async def connect_redis_with_retry(settings, *, logger) -> Redis:
    """Connect to Redis with the standard retry policy used by paper and live."""

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
    except Exception as exc:
        logger.warning("Neo4j unavailable: %s", exc)
        await driver.close()
        return None


async def connect_clickhouse_repositories(settings, *, logger):
    """Connect ClickHouse analytics first, with a narrow PostgreSQL journal fallback.

    ClickHouse remains mandatory for market data and analytical repositories.
    PostgreSQL can replace only the operational JournalRepository adapter;
    it is never presented as a substitute for ClickHouse analytics, market
    history, learning queries, or feature-store capabilities.
    """
    from aitos.data.repository import MarketDataRepository
    from aitos.journal.repository import JournalRepository

    market_repo = MarketDataRepository(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    storage = getattr(settings, "storage", None)
    explicit_postgres = getattr(storage, "backend", "auto") == "postgres"
    allow_fallback = bool(getattr(storage, "allow_fallback", False)) and getattr(storage, "backend", "auto") != "clickhouse"
    postgres_url = getattr(storage, "postgres_url", "") if storage else ""
    journal_repo = None

    try:
        await retry_with_backoff(
            lambda: market_repo.initialize({}),
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="ClickHouse market repository initialization",
        )
    except RetryExhaustedError as exc:
        await market_repo.shutdown()
        logger.error(
            "ClickHouse market/analytics storage is mandatory; PostgreSQL "
            "journal fallback cannot safely replace it: %s",
            exc,
        )
        raise SystemExit(1) from exc

    try:
        if not explicit_postgres:
            journal_repo = JournalRepository(
                host=settings.clickhouse.host,
                port=settings.clickhouse.port,
                username=settings.clickhouse.user,
                password=settings.clickhouse.password,
                database=settings.clickhouse.database,
            )
            await retry_with_backoff(
                lambda: journal_repo.initialize({}),
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                operation_name="ClickHouse journal repository initialization",
            )
            return market_repo, journal_repo

        if not allow_fallback:
            raise RuntimeError(
                "AITOS_STORAGE_BACKEND=postgres requires "
                "AITOS_STORAGE_ALLOW_FALLBACK=true"
            )
        raise RetryExhaustedError(1, RuntimeError("PostgreSQL explicitly selected"))

    except (RetryExhaustedError, RuntimeError) as clickhouse_error:
        if not allow_fallback or not postgres_url:
            await market_repo.shutdown()
            if journal_repo is not None:
                await journal_repo.shutdown()
            logger.error(
                "ClickHouse journal unavailable and PostgreSQL fallback is not "
                "enabled/configured: %s",
                clickhouse_error,
            )
            raise SystemExit(1) from clickhouse_error

        if journal_repo is not None:
            await journal_repo.shutdown()

        from aitos.journal.postgres_repository import PostgresJournalRepository

        postgres_repo = PostgresJournalRepository(postgres_url)
        try:
            await retry_with_backoff(
                lambda: postgres_repo.initialize({}),
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                operation_name="PostgreSQL operational journal initialization",
            )
        except RetryExhaustedError as postgres_error:
            await postgres_repo.shutdown()
            await market_repo.shutdown()
            logger.error(
                "Both ClickHouse journal and PostgreSQL operational journal "
                "are unavailable; refusing to start without durable journal: %s",
                postgres_error,
            )
            raise SystemExit(1) from postgres_error

        logger.warning(
            "Using PostgreSQL fallback for operational trade/journal records only; "
            "ClickHouse remains the market-data and analytics system of record"
        )
        return market_repo, postgres_repo
