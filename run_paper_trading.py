#!/usr/bin/env python3
"""AITOS paper-trading entrypoint with durable continual-learning experience capture."""

from __future__ import annotations

import asyncio
import signal

from aitos.app import (
    PaperPortfolioTracker,
    build_system,
    initialize_all,
    run_scan_and_trade_cycle,
    shutdown_all,
)
from aitos.config.settings import get_settings
from aitos.data.market_os_persistence import MarketOSPersistence
from aitos.data.repository import MarketDataRepository
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.execution.order_executor import SimulatedOrderExecutor
from aitos.health_server import HealthServer
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.journal.repository import JournalRepository
from aitos.learning.recorder import LearningExperienceRecorder
from aitos.logging_setup import configure_logging, get_logger
from aitos.resilience import RetryExhaustedError, retry_with_backoff
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.persistence import load_attention_model, save_attention_model

logger = get_logger("aitos.run_paper_trading")
SYMBOLS = ["BTCUSDT", "SOLUSDT"]
SCAN_INTERVAL_SECONDS = 60.0
KLINE_TIMEFRAME = "15m"
STARTING_EQUITY_USD = 10_000.0
HEALTH_SERVER_PORT = 8090
PAPER_MIN_SCORE_THRESHOLD = 50.0


async def connect_clickhouse_repositories(
    settings,
) -> tuple[MarketDataRepository, JournalRepository]:
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
            market_repo.initialize,
            max_attempts=5,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            operation_name="ClickHouse market repository initialization",
        )
        await retry_with_backoff(
            journal_repo.initialize,
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
    redis_client = await connect_redis_with_retry(settings)
    from aitos.eventbus.redis_bus import EventBus

    event_bus = EventBus(redis_client=redis_client)
    await event_bus.initialize({})
    market_repo, journal_repo = await connect_clickhouse_repositories(settings)
    graph_driver = await try_connect_neo4j(settings)
    rl_scorer = DeepValueRLScorer()
    rl_scorer.load_state()
    outcome_classifier = TradeOutcomeClassifier()
    outcome_classifier.load_state()
    attention_path = "/models/online_ml/attention_explainer.pkl"
    attention_explainer = load_attention_model(attention_path) or AttentionExplainer()
    components = await build_system(
        event_bus=event_bus,
        exchange=BinanceFuturesAdapter(),
        order_executor=SimulatedOrderExecutor(),
        symbols=SYMBOLS,
        kline_timeframe=KLINE_TIMEFRAME,
        scanner_timeframe=KLINE_TIMEFRAME,
        market_data_repository=market_repo,
        journal_repository=journal_repo,
        graph_driver=graph_driver,
        risk_limits=None,
        rl_scorer=rl_scorer,
        outcome_classifier=outcome_classifier,
        attention_explainer=attention_explainer,
        min_score_threshold=PAPER_MIN_SCORE_THRESHOLD,
        enable_exit_intelligence=True,
    )
    logger.info(
        "paper trading thresholds",
        extra={
            "aitos_extra": {
                "scanner_min_score_threshold": PAPER_MIN_SCORE_THRESHOLD,
                "kernel_min_confidence": components.kernel.fusion_min_confidence,
                "ai_threshold_relaxed": False,
            }
        },
    )
    await initialize_all(components)
    market_os_persistence = MarketOSPersistence(event_bus, market_repo)
    await market_os_persistence.initialize({})
    experience_recorder = LearningExperienceRecorder(
        event_bus, market_repo, source="paper"
    )
    await experience_recorder.initialize({})
    health_server = HealthServer(
        components.all_modules() + [experience_recorder, market_os_persistence],
        host="0.0.0.0",  # nosec B104 - required for Docker port forwarding
        port=HEALTH_SERVER_PORT,
    )
    await health_server.start()
    tracker = PaperPortfolioTracker(starting_equity_usd=STARTING_EQUITY_USD)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        while not stop_event.is_set():
            try:
                submitted = await run_scan_and_trade_cycle(components, tracker)
                rl_scorer.save_state()
                outcome_classifier.save_state()
                save_attention_model(attention_explainer, attention_path)
                logger.info(
                    "scan cycle complete",
                    extra={
                        "aitos_extra": {
                            "submitted": submitted,
                            "open_trades": len(
                                components.trade_lifecycle.get_open_trades()
                            ),
                            "closed_trades": len(
                                components.trade_lifecycle.get_closed_trades()
                            ),
                            "rl_samples": rl_scorer.n_samples_seen,
                            "ml_samples": outcome_classifier.n_samples_seen,
                            "attention_samples": attention_explainer.n_samples_seen,
                        }
                    },
                )
            except Exception as exc:
                logger.error("scan cycle failed: %s", exc)
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
        await market_os_persistence.shutdown()
        await shutdown_all(components)
        await market_repo.shutdown()
        await journal_repo.shutdown()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
