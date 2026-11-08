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
    """Connect ClickHouse analytics and capability-checked operational journal.

    ClickHouse remains mandatory for market history and analytics. The
    PostgreSQL adapter is eligible only for the narrower trade/journal
    capability set it actually implements.
    """
    from aitos.data.repository import MarketDataRepository
    from aitos.journal.repository import JournalRepository
    from aitos.storage.backend import (
        BackendAdapter,
        StorageBackend,
        StorageBackendError,
        select_storage_backend,
    )

    market_repo = MarketDataRepository(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    storage = getattr(settings, "storage", None)
    backend_mode = getattr(storage, "backend", "auto")
    primary_name = (
        "postgres"
        if backend_mode == "postgres"
        else (
            "clickhouse"
            if backend_mode == "clickhouse"
            else getattr(storage, "primary", "clickhouse")
        )
    )
    fallback_name = getattr(storage, "fallback", "postgres")
    allow_fallback = (
        bool(getattr(storage, "allow_fallback", False))
        and backend_mode == "auto"
        and fallback_name != primary_name
    )
    postgres_url = getattr(storage, "postgres_url", "") if storage else ""
    required = frozenset(
        {
            "trade_snapshots",
            "journal_entries",
            "decision_journal",
            "outcome_attribution",
        }
    )

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

    clickhouse_journal = JournalRepository(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )

    async def probe_clickhouse_journal() -> None:
        try:
            await retry_with_backoff(
                lambda: clickhouse_journal.initialize({}),
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                operation_name="ClickHouse journal repository initialization",
            )
        except Exception:
            await clickhouse_journal.shutdown()
            raise

    adapters = {
        StorageBackend.CLICKHOUSE: BackendAdapter(
            backend=StorageBackend.CLICKHOUSE,
            capabilities=required,
            probe=probe_clickhouse_journal,
        )
    }
    postgres_journal = None
    if postgres_url:
        from aitos.journal.postgres_repository import PostgresJournalRepository

        postgres_journal = PostgresJournalRepository(postgres_url)

        async def probe_postgres_journal() -> None:
            try:
                await retry_with_backoff(
                    lambda: postgres_journal.initialize({}),
                    max_attempts=5,
                    base_delay_seconds=2.0,
                    max_delay_seconds=30.0,
                    operation_name="PostgreSQL operational journal initialization",
                )
            except Exception:
                await postgres_journal.shutdown()
                raise

        adapters[StorageBackend.POSTGRES] = BackendAdapter(
            backend=StorageBackend.POSTGRES,
            capabilities=PostgresJournalRepository.CAPABILITIES,
            probe=probe_postgres_journal,
        )

    try:
        primary = StorageBackend(primary_name)
        fallback = StorageBackend(fallback_name)
        selection = await select_storage_backend(
            adapters=adapters,
            required_capabilities=required,
            primary=primary,
            fallback=fallback,
            allow_fallback=allow_fallback,
        )
    except (StorageBackendError, ValueError) as exc:
        await market_repo.shutdown()
        await clickhouse_journal.shutdown()
        if postgres_journal is not None:
            await postgres_journal.shutdown()
        logger.error(
            "No configured journal backend can satisfy the operational "
            "persistence contract; refusing to start: %s",
            exc,
        )
        raise SystemExit(1) from exc

    if selection.backend is StorageBackend.CLICKHOUSE:
        if postgres_journal is not None:
            await postgres_journal.shutdown()
        return market_repo, clickhouse_journal

    await clickhouse_journal.shutdown()
    logger.warning(
        "Using PostgreSQL fallback for operational trade/journal records only; "
        "ClickHouse remains the market-data and analytics system of record"
    )
    return market_repo, postgres_journal
