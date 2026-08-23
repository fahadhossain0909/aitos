#!/usr/bin/env python3
"""AITOS LIVE trading entrypoint with persistent market/decision/learning data."""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from redis.asyncio import Redis

from aitos.app import (LivePortfolioTracker, build_system, initialize_all,
                       run_scan_and_trade_cycle, shutdown_all)
from aitos.config.settings import get_settings
from aitos.data.repository import MarketDataRepository
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.health_server import HealthServer
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.journal.repository import JournalRepository
from aitos.kernel.ai_kernel import AIKernel
from aitos.learning.recorder import LearningExperienceRecorder
from aitos.live_trading import confirm_live_trading, prepare_live_executor
from aitos.logging_setup import configure_logging, get_logger
from aitos.resilience import RetryExhaustedError, retry_with_backoff
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.persistence import load_attention_model, save_attention_model

logger = get_logger("aitos.run_live_trading")
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SCAN_INTERVAL_SECONDS = 60.0
KLINE_TIMEFRAME = "15m"
HEALTH_SERVER_PORT = 8091


async def connect_redis_with_retry(settings) -> Redis:
    async def _attempt() -> Redis:
        client = Redis.from_url(settings.redis.url)
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


async def try_connect_clickhouse_repositories(settings):
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
        logger.error("ClickHouse persistence unavailable: %s", exc)
        return None, None


async def try_connect_neo4j(settings):
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        settings.neo4j.uri, auth=(settings.neo4j.user, settings.neo4j.password)
    )
    try:
        await driver.verify_connectivity()
        return driver
    except Exception as exc:
        logger.warning("Neo4j unavailable: %s", exc)
        await driver.close()
        return None


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    approved_by = confirm_live_trading(SYMBOLS, testnet=settings.binance.testnet)
    redis_client = await connect_redis_with_retry(settings)
    from aitos.eventbus.redis_bus import EventBus

    event_bus = EventBus(redis_client=redis_client)
    await event_bus.initialize({})
    market_repo, journal_repo = await try_connect_clickhouse_repositories(settings)
    graph_driver = await try_connect_neo4j(settings)
    order_executor = await prepare_live_executor(settings, SYMBOLS)
    rl_scorer = DeepValueRLScorer()
    rl_scorer.load_state()
    outcome_classifier = TradeOutcomeClassifier()
    outcome_classifier.load_state()
    attention_path = "models/online_ml/attention_explainer.pkl"
    attention_explainer = load_attention_model(attention_path) or AttentionExplainer()
    components = await build_system(
        event_bus=event_bus,
        exchange=BinanceFuturesAdapter(),
        order_executor=order_executor,
        symbols=SYMBOLS,
        kline_timeframe=KLINE_TIMEFRAME,
        scanner_timeframe=KLINE_TIMEFRAME,
        market_data_repository=market_repo,
        journal_repository=journal_repo,
        graph_driver=graph_driver,
        kernel=AIKernel(event_bus=event_bus, require_human_approval_for_prod=True),
        rl_scorer=rl_scorer,
        outcome_classifier=outcome_classifier,
        attention_explainer=attention_explainer,
        use_exchange_side_stops=True,
    )
    await initialize_all(components)
    experience_recorder = LearningExperienceRecorder(
        event_bus, market_repo, source="live"
    )
    await experience_recorder.initialize({})
    # The container publishes 127.0.0.1:8091 to the host. Bind inside the
    # container to all interfaces so Docker's port-forward can reach it.
    health_server = HealthServer(
        components.all_modules() + [experience_recorder],
        host="0.0.0.0",
        port=HEALTH_SERVER_PORT,
    )
    await health_server.start()
    tracker = LivePortfolioTracker(order_executor=order_executor)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        while not stop_event.is_set():
            try:
                submitted = await run_scan_and_trade_cycle(
                    components, tracker, is_production=True, approved_by=approved_by
                )
                rl_scorer.save_state()
                outcome_classifier.save_state()
                save_attention_model(attention_explainer, attention_path)
                logger.info(
                    "live scan cycle complete",
                    extra={
                        "aitos_extra": {
                            "submitted": submitted,
                            "open_trades": len(
                                components.trade_lifecycle.get_open_trades()
                            ),
                            "account_equity_usd": tracker._last_known_equity_usd,
                            "rl_samples": rl_scorer.n_samples_seen,
                            "ml_samples": outcome_classifier.n_samples_seen,
                            "attention_samples": attention_explainer.n_samples_seen,
                        }
                    },
                )
                if components.reconciliation is not None:
                    await components.reconciliation.run_once()
            except Exception as exc:
                logger.error("scan/trade cycle failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        rl_scorer.save_state()
        outcome_classifier.save_state()
        save_attention_model(attention_explainer, attention_path)
        await health_server.stop()
        await experience_recorder.shutdown()
        await shutdown_all(components)
        await order_executor.close()
        if market_repo is not None:
            await market_repo.shutdown()
        if journal_repo is not None:
            await journal_repo.shutdown()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
