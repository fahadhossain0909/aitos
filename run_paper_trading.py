#!/usr/bin/env python3
"""AITOS paper-trading entrypoint with durable continual-learning experience capture."""

from __future__ import annotations

import asyncio
import math

from aitos.agents import LearningAgent, MarketAgent, PortfolioAgent, RiskAgent
from aitos.app import (
    PaperPortfolioTracker,
    build_system,
    initialize_all,
    run_scan_and_trade_cycle,
    shutdown_all,
)
from aitos.config.paths import get_model_data_dir
from aitos.config.settings import get_settings
from aitos.data.market_os_persistence import MarketOSPersistence
from aitos.data.protected_position_monitor import (
    install_protected_position_monitor,
    set_open_position_provider,
)
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.execution.order_executor import SimulatedOrderExecutor
from aitos.health_server import HealthServer
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.learning.recorder import LearningExperienceRecorder
from aitos.learning.worker import ContinualLearningWorker
from aitos.logging_setup import configure_logging, get_logger
from aitos.market_data.universe import resolve_live_universe
from aitos.runtime_connections import (
    connect_clickhouse_repositories,
    connect_redis_with_retry,
)
from aitos.runtime_signals import install_signal_handlers
from aitos.trading.persistent_state import (
    DurableTradingStateStore,
    TradeStatePersistence,
)
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.persistence import load_attention_model, save_attention_model

logger = get_logger("aitos.run_paper_trading")
SCAN_INTERVAL_SECONDS = get_settings().tuning.scanner.refresh_interval_seconds
KLINE_TIMEFRAME = "15m"
STARTING_EQUITY_USD = 1_000.0
HEALTH_SERVER_PORT = 8090
PAPER_MIN_SCORE_THRESHOLD = 50.0
STALE_POSITION_MAX_HOURS = 72


async def _close_stale_positions(components) -> None:
    """Close open trades held longer than STALE_POSITION_MAX_HOURS."""
    from datetime import datetime, timezone

    from aitos.models.trade import TradeLifecycleState

    now = datetime.now(timezone.utc)
    for trade in list(components.trade_lifecycle.get_open_trades()):
        if trade.state != TradeLifecycleState.POSITION_OPENED:
            continue
        try:
            entry_dt = datetime.fromisoformat(trade.entry_time)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            age_hours = (now - entry_dt).total_seconds() / 3600
            if age_hours >= STALE_POSITION_MAX_HOURS:
                # Never synthesize a close price from entry price: doing so
                # fabricates a zero-PnL close when the market feed is stale or
                # the process has just recovered a position. Keep the position
                # open and observable until a valid mark is available.
                last_price = getattr(trade, "last_marked_price", None)
                if (
                    not isinstance(last_price, (int, float))
                    or not math.isfinite(last_price)
                    or last_price <= 0
                ):
                    logger.warning(
                        "stale position close deferred: no valid marked price",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "age_hours": round(age_hours, 1),
                            }
                        },
                    )
                    continue
                await components.trade_lifecycle.close_trade(
                    trade.trade_id, float(last_price), "stale_position_timeout"
                )
                logger.warning(
                    "stale position auto-closed",
                    extra={
                        "aitos_extra": {
                            "trade_id": trade.trade_id,
                            "symbol": trade.symbol,
                            "age_hours": round(age_hours, 1),
                        }
                    },
                )
        except Exception:
            logger.exception(
                "stale position check failed",
                extra={"aitos_extra": {"trade_id": trade.trade_id}},
            )


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
    if settings.tuning.mode != "paper":
        raise RuntimeError(
            "Trading entrypoint/configuration mode mismatch: run_paper_trading.py requires AITOS_MODE=paper"
        )
    configure_logging(settings.log_level)
    redis_client = await connect_redis_with_retry(settings, logger=logger)
    from aitos.eventbus.redis_bus import EventBus

    event_bus = EventBus(redis_client=redis_client)
    await event_bus.initialize({})
    market_repo, journal_repo = await connect_clickhouse_repositories(
        settings, logger=logger
    )
    graph_driver = await try_connect_neo4j(settings)
    exchange = BinanceFuturesAdapter()
    symbols = await resolve_live_universe(exchange)
    logger.info(
        "resolved dynamic paper-trading universe",
        extra={"aitos_extra": {"symbol_count": len(symbols)}},
    )
    rl_scorer = DeepValueRLScorer()
    rl_scorer.load_state()
    outcome_classifier = TradeOutcomeClassifier()
    outcome_classifier.load_state()
    attention_path = str(get_model_data_dir() / "online_ml" / "attention_explainer.pkl")
    attention_explainer = load_attention_model(attention_path) or AttentionExplainer()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=SimulatedOrderExecutor(),
        symbols=symbols,
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
    state_store = DurableTradingStateStore(market_repo)
    trade_state_persistence = TradeStatePersistence(
        event_bus, components.trade_lifecycle, state_store
    )
    # Ensure runtime-state tables and event subscriptions exist before recovery.
    # Restoring before initialization could query a table that has not yet been
    # created on a fresh/recreated ClickHouse volume.
    await trade_state_persistence.initialize()
    set_open_position_provider(components.trade_lifecycle.get_open_trades)
    # Install protection before recovery/resubscription so every subsequent
    # scanner-driven subscription update preserves restored position symbols.
    install_protected_position_monitor()
    restored_count = await trade_state_persistence.restore()
    logger.info(
        "paper lifecycle recovery initialized",
        extra={
            "aitos_extra": {
                "restored_open_trades": restored_count,
            }
        },
    )
    await initialize_all(components)
    # Canonical market streams only become active during initialize_all().
    # Reconcile subscriptions afterwards and verify the effective symbol sets.
    await trade_state_persistence.resubscribe_restored_positions()

    # Start continual-learning worker to incrementally train on historical
    # backtest experiences stored in ClickHouse. Runs in a background thread
    # because its run_forever() loop is synchronous/blocking.
    learning_worker = ContinualLearningWorker(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        user=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    learning_task = asyncio.create_task(asyncio.to_thread(learning_worker.run_forever))
    learning_task.set_name("continual-learning-worker")

    # Register AI Kernel agents for multi-agent consensus
    market_agent = MarketAgent(event_bus=event_bus)
    risk_agent = RiskAgent(event_bus=event_bus)
    portfolio_agent = PortfolioAgent(event_bus=event_bus)
    learning_agent = LearningAgent(event_bus=event_bus)

    for agent in (market_agent, risk_agent, portfolio_agent, learning_agent):
        await agent.initialize({})
        await components.kernel.register_agent(agent)
        logger.info(
            "registered AI agent",
            extra={
                "aitos_extra": {
                    "agent_id": agent.module_id,
                    "weight": agent.consensus_weight,
                }
            },
        )
    logger.info(
        "AI Kernel agents registered",
        extra={
            "aitos_extra": {"agents": components.kernel._world_state.registered_agents}
        },
    )

    market_os_persistence = MarketOSPersistence(event_bus, market_repo)
    await market_os_persistence.initialize({})
    experience_recorder = LearningExperienceRecorder(
        event_bus, market_repo, source="paper"
    )
    await experience_recorder.initialize({})
    health_server = HealthServer(
        components.all_modules()
        + [
            experience_recorder,
            market_os_persistence,
            market_agent,
            risk_agent,
            portfolio_agent,
            learning_agent,
        ],
        # The health endpoint is intentionally container/network reachable.
        # nosec B104 - binding all interfaces is required for the Docker health check.
        host="0.0.0.0",  # nosec B104
        port=HEALTH_SERVER_PORT,
    )
    await health_server.start()

    # Self-healing: periodically re-initialize modules that have died
    async def _self_heal_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(300)  # Check every 5 minutes
            for module in components.all_modules():
                try:
                    health = await module.health_check()
                    if health.status.value == "unhealthy":
                        logger.warning(
                            "self-healing: restarting unhealthy module",
                            extra={"aitos_extra": {"module": module.module_id}},
                        )
                        await module.initialize({})
                except Exception as exc:
                    logger.error(
                        "self-healing: failed to re-initialize module",
                        extra={
                            "aitos_extra": {
                                "module": getattr(module, "module_id", "unknown"),
                                "error": str(exc),
                            }
                        },
                    )

    heal_task = asyncio.create_task(_self_heal_loop())
    from aitos.config.settings import get_settings as _get_settings
    from aitos.intelligence.position_runtime import get_tracked_lifecycles
    from aitos.trading.price_safety_net import PositionPriceSafetyNet

    _settings = _get_settings()
    price_safety_net = PositionPriceSafetyNet(
        exchange,
        get_tracked_lifecycles,
        stale_symbol_blacklist=_settings.stale_symbol_blacklist,
    )
    price_safety_net.start()
    tracker = PaperPortfolioTracker(starting_equity_usd=STARTING_EQUITY_USD)
    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)
    try:
        while not stop_event.is_set():
            try:
                submitted = await run_scan_and_trade_cycle(components, tracker)
                # Attempt circuit breaker recovery after each scan cycle so a
                # stale OPEN state doesn't block new entries forever. A
                # transient spike (API blip, stale data) can pause trading
                # until someone manually intervenes without this.
                recovered = await components.risk_engine.attempt_recovery()
                if recovered:
                    # Scan cycle succeeded while HALF_OPEN → confirm recovery
                    components.risk_engine.circuit_breaker.record_probe_result(
                        True, "scan cycle success"
                    )
                # These do blocking pickle.dump()+file-replace I/O. Run them
                # off the event loop -- a synchronous call here previously
                # stalled *all* concurrent async work (websocket recv,
                # event-bus consumption) for however long the disk write
                # took, and a stall past WS_PING_TIMEOUT_SECONDS could drop
                # the market-data connection entirely.
                await asyncio.to_thread(rl_scorer.save_state)
                await asyncio.to_thread(outcome_classifier.save_state)
                await asyncio.to_thread(
                    save_attention_model, attention_explainer, attention_path
                )
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

                # Close stale positions (holding > STALE_POSITION_MAX_HOURS)
                await _close_stale_positions(components)
            except Exception as exc:
                logger.error("scan cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=get_settings().tuning.scanner.refresh_interval_seconds,
                )
            except TimeoutError:
                pass
    finally:
        heal_task.cancel()
        try:
            await heal_task
        except asyncio.CancelledError:
            pass
        # Gracefully stop the continual-learning worker
        learning_task.cancel()
        try:
            await learning_task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(learning_worker.shutdown)
        await price_safety_net.stop()
        await asyncio.to_thread(rl_scorer.save_state)
        await asyncio.to_thread(outcome_classifier.save_state)
        await asyncio.to_thread(
            save_attention_model, attention_explainer, attention_path
        )
        await health_server.stop()
        await experience_recorder.shutdown()
        await market_os_persistence.shutdown()
        await trade_state_persistence.shutdown()
        await shutdown_all(components)
        await market_repo.shutdown()
        await journal_repo.shutdown()
        if graph_driver is not None:
            await graph_driver.close()
        await redis_client.close()


if __name__ == "__main__":
    asyncio.run(main())
