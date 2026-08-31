#!/usr/bin/env python3
"""AITOS paper-trading entrypoint with durable continual-learning experience capture."""

from __future__ import annotations

from aitos.data.repository import MarketDataRepository
from aitos.journal.repository import JournalRepository
from aitos.logging_setup import get_logger

logger = get_logger("aitos.run_paper_trading")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
SCAN_INTERVAL_SECONDS = 60.0
KLINE_TIMEFRAME = "15m"
STARTING_EQUITY_USD = 10_000.0
HEALTH_SERVER_PORT = 8090
# Deliberately relaxed for paper-only functional validation. Live/production
# defaults remain unchanged until the signal path has been validated.
PAPER_MIN_SCORE_THRESHOLD = 50.0


async def try_connect_clickhouse_repositories(
    settings,
) -> tuple[MarketDataRepository | None, JournalRepository | None]:
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
        await market_repo.initialize({})
        await journal_repo.initialize({})
        return market_repo, journal_repo
    except Exception as exc:
        logger.warning("ClickHouse unavailable: %s", exc)
        return None, None


async def try_connect_neo4j(settings):
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        settings.neo4j.uri, auth=(settings.neo4j.user, settings.neo4j.password)
    )
    try:
        await driver.verify_connectivity()
        return driver
    except Exception as exp:
        logger.warning("Neo4j unavailable: %s", exp)
        await driver.close()
        return None
