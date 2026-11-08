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
    """Connect primary storage with an explicitly bounded PostgreSQL fallback.

    ClickHouse is the full analytical system of record. PostgreSQL-only mode
    supports operational paper market records and journals, but arbitrary
    ClickHouse analytical queries remain unsupported and are never faked.
    """
    from aitos.data.repository import MarketDataRepository
    from aitos.journal.repository import JournalRepository
    from aitos.storage.backend import (
        BackendAdapter,
        StorageBackend,
        StorageBackendError,
        select_storage_backend,
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
        and primary_name == "clickhouse"
        and fallback_name == "postgres"
    )
    postgres_url = getattr(storage, "postgres_url", "") if storage else ""
    require_analytics = bool(getattr(storage, "require_clickhouse_analytics", True))

    market_repo = None
    market_backend = StorageBackend(primary_name)
    if market_backend is StorageBackend.POSTGRES:
        if not postgres_url:
            logger.error(
                "PostgreSQL is primary but AITOS_STORAGE_POSTGRES_URL is empty"
            )
            raise SystemExit(1)
        if require_analytics:
            logger.error(
                "PostgreSQL-only paper mode requires "
                "AITOS_STORAGE_REQUIRE_CLICKHOUSE_ANALYTICS=false; "
                "ClickHouse analytical queries are not emulated"
            )
            raise SystemExit(1)
        from aitos.data.postgres_repository import PostgresMarketDataRepository

        market_repo = PostgresMarketDataRepository(postgres_url)
        try:
            await retry_with_backoff(
                lambda: market_repo.initialize({}),
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                operation_name="PostgreSQL paper market repository initialization",
            )
        except RetryExhaustedError as exc:
            await market_repo.shutdown()
            logger.error("PostgreSQL paper market storage unavailable: %s", exc)
            raise SystemExit(1) from exc
        logger.warning(
            "PostgreSQL-only paper mode active: market events, recent OHLCV and "
            "operational journals are persisted; ClickHouse analytical SQL is disabled"
        )
    else:
        market_repo = MarketDataRepository(
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
        except RetryExhaustedError as clickhouse_error:
            await market_repo.shutdown()
            if not allow_fallback or fallback_name != "postgres" or not postgres_url:
                logger.error(
                    "ClickHouse market/analytics storage unavailable and no "
                    "configured PostgreSQL fallback is eligible: %s",
                    clickhouse_error,
                )
                raise SystemExit(1) from clickhouse_error
            if require_analytics:
                logger.error(
                    "PostgreSQL market fallback is available, but "
                    "AITOS_STORAGE_REQUIRE_CLICKHOUSE_ANALYTICS=true; "
                    "refusing to disable ClickHouse analytical capabilities"
                )
                raise SystemExit(1) from clickhouse_error
            from aitos.data.postgres_repository import PostgresMarketDataRepository

            market_repo = PostgresMarketDataRepository(postgres_url)
            try:
                await retry_with_backoff(
                    lambda: market_repo.initialize({}),
                    max_attempts=5,
                    base_delay_seconds=2.0,
                    max_delay_seconds=30.0,
                    operation_name="PostgreSQL paper market repository initialization",
                )
            except RetryExhaustedError as postgres_error:
                await market_repo.shutdown()
                logger.error(
                    "ClickHouse and PostgreSQL market storage are unavailable: %s",
                    postgres_error,
                )
                raise SystemExit(1) from postgres_error
            market_backend = StorageBackend.POSTGRES
            logger.warning(
                "ClickHouse unavailable; using PostgreSQL paper-runtime storage. "
                "Arbitrary ClickHouse analytics queries remain disabled."
            )

    from aitos.journal.postgres_repository import PostgresJournalRepository

    postgres_journal = PostgresJournalRepository(postgres_url) if postgres_url else None
    clickhouse_journal = None
    required_journal = frozenset(
        {
            "trade_snapshots",
            "journal_entries",
            "decision_journal",
            "outcome_attribution",
        }
    )

    async def probe_postgres_journal() -> None:
        if postgres_journal is None:
            raise RuntimeError("PostgreSQL journal adapter is not configured")
        await retry_with_backoff(
            lambda: postgres_journal.initialize({}),
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="PostgreSQL operational journal initialization",
        )

    if market_backend is StorageBackend.POSTGRES:
        journal_adapters = {
            StorageBackend.POSTGRES: BackendAdapter(
                backend=StorageBackend.POSTGRES,
                capabilities=PostgresJournalRepository.CAPABILITIES,
                probe=probe_postgres_journal,
            )
        }
        journal_primary = StorageBackend.POSTGRES
        journal_fallback = StorageBackend.POSTGRES
        journal_allow_fallback = False
    else:
        clickhouse_journal = JournalRepository(
            host=settings.clickhouse.host,
            port=settings.clickhouse.port,
            username=settings.clickhouse.user,
            password=settings.clickhouse.password,
            database=settings.clickhouse.database,
        )

        async def probe_clickhouse_journal() -> None:
            await retry_with_backoff(
                lambda: clickhouse_journal.initialize({}),
                max_attempts=5,
                base_delay_seconds=2.0,
                max_delay_seconds=30.0,
                operation_name="ClickHouse journal repository initialization",
            )

        journal_adapters = {
            StorageBackend.CLICKHOUSE: BackendAdapter(
                backend=StorageBackend.CLICKHOUSE,
                capabilities=required_journal,
                probe=probe_clickhouse_journal,
            )
        }
        if postgres_journal is not None:
            journal_adapters[StorageBackend.POSTGRES] = BackendAdapter(
                backend=StorageBackend.POSTGRES,
                capabilities=PostgresJournalRepository.CAPABILITIES,
                probe=probe_postgres_journal,
            )
        journal_primary = StorageBackend.CLICKHOUSE
        journal_fallback = StorageBackend.POSTGRES
        journal_allow_fallback = allow_fallback

    try:
        journal_selection = await select_storage_backend(
            adapters=journal_adapters,
            required_capabilities=required_journal,
            primary=journal_primary,
            fallback=journal_fallback,
            allow_fallback=journal_allow_fallback,
        )
    except (StorageBackendError, ValueError) as exc:
        await market_repo.shutdown()
        if clickhouse_journal is not None:
            await clickhouse_journal.shutdown()
        if postgres_journal is not None:
            await postgres_journal.shutdown()
        logger.error(
            "No configured journal backend can satisfy the operational "
            "persistence contract; refusing to start: %s",
            exc,
        )
        raise SystemExit(1) from exc

    if journal_selection.backend is StorageBackend.CLICKHOUSE:
        if postgres_journal is not None:
            await postgres_journal.shutdown()
        return market_repo, clickhouse_journal

    if clickhouse_journal is not None:
        await clickhouse_journal.shutdown()
    logger.warning(
        "Using PostgreSQL for operational trade, decision and outcome journals"
    )
    return market_repo, postgres_journal
