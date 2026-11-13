#!/usr/bin/env python3
"""AITOS paper-trading entrypoint with durable continual-learning experience capture."""

from __future__ import annotations

import asyncio
import math
import os
from contextlib import AsyncExitStack

from aitos.agents import LearningAgent, MarketAgent, PortfolioAgent, RiskAgent
from aitos.app import (
    PaperPortfolioTracker,
    build_system,
    initialize_all,
    restart_unhealthy_modules,
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
    try_connect_neo4j,
)
from aitos.runtime_signals import install_signal_handlers
from aitos.trading.persistent_state import (
    DurableTradingStateStore,
    IdempotentOrderExecutor,
    TradeStatePersistence,
    restored_position_feeds_ready,
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
STALE_POSITION_MAX_MARK_AGE_SECONDS = 300.0
STALE_POSITION_MAX_HOURS = get_settings().tuning.positions.stale_position_max_hours
DEAD_CANDLE_MAX_PROGRESS_R = get_settings().tuning.positions.dead_candle_max_progress_r
DEAD_CANDLE_ENTRY_BAND_PCT = get_settings().tuning.positions.dead_candle_entry_band_pct


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
                # This paper-runtime backstop is deliberately limited to a
                # verified flatline. Active positions and recovered positions
                # without excursion evidence must remain under EIE monitoring.
                last_price = getattr(trade, "last_marked_price", None)
                if (
                    not isinstance(last_price, (int, float))
                    or not math.isfinite(last_price)
                    or last_price <= 0
                ):
                    logger.warning(
                        "dead-candle timeout deferred: no valid marked price",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "age_hours": round(age_hours, 1),
                            }
                        },
                    )
                    continue

                last_marked_at = getattr(trade, "last_marked_at", None)
                try:
                    mark_dt = datetime.fromisoformat(
                        str(last_marked_at).replace("Z", "+00:00")
                    )
                    if mark_dt.tzinfo is None:
                        mark_dt = mark_dt.replace(tzinfo=timezone.utc)
                    mark_age_seconds = (now - mark_dt).total_seconds()
                except (TypeError, ValueError):
                    mark_age_seconds = float("inf")
                if (
                    not math.isfinite(mark_age_seconds)
                    or mark_age_seconds < 0
                    or mark_age_seconds > STALE_POSITION_MAX_MARK_AGE_SECONDS
                ):
                    logger.warning(
                        "dead-candle timeout deferred: market mark is stale",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "mark_age_seconds": (
                                    round(mark_age_seconds, 1)
                                    if math.isfinite(mark_age_seconds)
                                    else None
                                ),
                            }
                        },
                    )
                    continue

                entry_price = float(getattr(trade, "entry_price", 0.0) or 0.0)
                mae_price = getattr(trade, "mae_price", None)
                mfe_price = getattr(trade, "mfe_price", None)
                mfe_r = getattr(trade, "mfe_r", None)
                # Missing path telemetry is not proof of a flatline.
                if (
                    not math.isfinite(entry_price)
                    or entry_price <= 0
                    or not isinstance(mfe_r, (int, float))
                    or not math.isfinite(mfe_r)
                    or not isinstance(mae_price, (int, float))
                    or not math.isfinite(mae_price)
                    or not isinstance(mfe_price, (int, float))
                    or not math.isfinite(mfe_price)
                ):
                    logger.warning(
                        "dead-candle timeout deferred: excursion evidence unavailable",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "age_hours": round(age_hours, 1),
                            }
                        },
                    )
                    continue

                max_excursion_pct = (
                    max(float(mae_price), float(mfe_price)) / entry_price
                )
                current_deviation_pct = (
                    abs(float(last_price) - entry_price) / entry_price
                )
                progress_r = float(mfe_r)
                if (
                    max_excursion_pct > DEAD_CANDLE_ENTRY_BAND_PCT
                    or current_deviation_pct > DEAD_CANDLE_ENTRY_BAND_PCT
                    or progress_r >= DEAD_CANDLE_MAX_PROGRESS_R
                ):
                    continue

                await components.trade_lifecycle.close_trade(
                    trade.trade_id, float(last_price), "time_decay_scratch"
                )
                logger.warning(
                    "dead flatline position scratch-closed",
                    extra={
                        "aitos_extra": {
                            "trade_id": trade.trade_id,
                            "symbol": trade.symbol,
                            "age_hours": round(age_hours, 1),
                            "max_excursion_pct": round(max_excursion_pct * 100.0, 4),
                            "current_deviation_pct": round(
                                current_deviation_pct * 100.0, 4
                            ),
                            "mfe_r": round(progress_r, 4),
                        }
                    },
                )
        except Exception:
            logger.exception(
                "stale position check failed",
                extra={"aitos_extra": {"trade_id": trade.trade_id}},
            )


async def _cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("background task failed before shutdown")


async def _shutdown_event_bus_if_unowned(event_bus, ownership) -> None:
    if not ownership["components"]:
        await event_bus.shutdown()


async def _stop_learning_worker(worker, task: asyncio.Task) -> None:
    worker.request_stop()
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.warning("continual-learning worker task was cancelled during shutdown")
    except Exception:
        logger.exception("continual-learning worker exited with an error")
    await asyncio.to_thread(worker.shutdown)


async def _shutdown_agents(agents) -> None:
    for agent in reversed(agents):
        try:
            await agent.shutdown()
        except Exception:
            logger.exception(
                "failed to shut down auxiliary agent",
                extra={
                    "aitos_extra": {"agent_id": getattr(agent, "module_id", "unknown")}
                },
            )


async def _start_optional_hawkes_bootstrap(components):
    """Fit and OOS-gate per-symbol Hawkes filters before live event scoring.

    This lifecycle is opt-in and requires an explicit symbol allow-list. Models
    that fail convergence, stationarity, sample sufficiency or held-out
    Poisson-baseline comparison are not registered.
    """
    enabled = os.getenv("AITOS_HAWKES_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return ()

    symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv("AITOS_HAWKES_SYMBOLS", "").split(",")
        if symbol.strip()
    )
    if not symbols:
        raise RuntimeError(
            "AITOS_HAWKES_ENABLED is true but AITOS_HAWKES_SYMBOLS is empty"
        )
    scanner = next(
        (
            module
            for module in components.all_modules()
            if getattr(module, "module_id", None) == "opportunity-scanner"
        ),
        None,
    )
    if scanner is None:
        raise RuntimeError("opportunity-scanner unavailable for Hawkes bootstrap")

    from aitos.statistics.hawkes_runtime import bootstrap_hawkes_models

    decay_grid = tuple(
        float(value)
        for value in os.getenv("AITOS_HAWKES_DECAY_GRID", "0.05,0.1,0.2,0.5,1.0").split(
            ","
        )
        if value.strip()
    )
    results = await bootstrap_hawkes_models(
        scanner._exchange,
        scanner._a_stat_bridge.engine,
        symbols,
        sample_limit=int(os.getenv("AITOS_HAWKES_SAMPLE_LIMIT", "1000")),
        min_events=int(os.getenv("AITOS_HAWKES_MIN_EVENTS", "200")),
        holdout_fraction=float(os.getenv("AITOS_HAWKES_HOLDOUT_FRACTION", "0.2")),
        min_mark_events=int(os.getenv("AITOS_HAWKES_MIN_MARK_EVENTS", "20")),
        min_oos_gain_per_event=float(
            os.getenv("AITOS_HAWKES_MIN_OOS_GAIN_PER_EVENT", "0")
        ),
        decay_grid=decay_grid,
        max_iterations=int(os.getenv("AITOS_HAWKES_MAX_ITERATIONS", "120")),
    )
    logger.info(
        "A-Stat Hawkes bootstrap completed",
        extra={
            "aitos_extra": {
                "results": [
                    {
                        "symbol": item.symbol,
                        "status": item.status,
                        "events": item.events,
                        "training_events": item.training_events,
                        "holdout_events": item.holdout_events,
                        "oos_gain_per_event": item.oos_gain_per_event,
                        "branching_ratio": item.branching_ratio,
                        "reason": item.reason,
                    }
                    for item in results
                ],
                "decision_policy": "telemetry_only_until_separate_strategy_approval",
            }
        },
    )
    return results


async def _start_optional_statistical_drift_monitor(
    components, market_repo, settings, cleanup: AsyncExitStack
):
    """Start fail-closed drift monitoring only when explicitly configured.

    The feature allow-list is deployment-owned because learning experience
    schemas differ between strategies. No feature or alert endpoint is guessed.
    """
    enabled = os.getenv("AITOS_STATISTICAL_DRIFT_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None, None

    feature_names = tuple(
        name.strip()
        for name in os.getenv("AITOS_STATISTICAL_DRIFT_FEATURES", "").split(",")
        if name.strip()
    )
    if not feature_names:
        raise RuntimeError(
            "statistical drift is enabled but AITOS_STATISTICAL_DRIFT_FEATURES "
            "is empty; refusing to monitor an undefined feature schema"
        )

    import clickhouse_connect

    from aitos.learning.clickhouse_store import ClickHouseExperienceStore
    from aitos.statistics.governance_runtime import StatisticalGovernanceRuntime
    from aitos.statistics.production_drift import (
        ClickHouseDriftFeatureBatchProvider,
        LoggingDriftAlertSink,
        WebhookDriftAlertSink,
    )
    from aitos.statistics.registry import StatisticalModelRegistry

    # Use a dedicated synchronous client because the provider is executed on
    # the scheduler's worker task and must not block the paper event loop.
    client = await asyncio.to_thread(
        clickhouse_connect.get_client, **market_repo._conn_params
    )
    cleanup.callback(client.close)
    store = ClickHouseExperienceStore(client, database=settings.clickhouse.database)
    provider = ClickHouseDriftFeatureBatchProvider(
        store,
        feature_names=feature_names,
        min_observations=int(
            os.getenv("AITOS_STATISTICAL_DRIFT_MIN_OBSERVATIONS", "200")
        ),
        sources=tuple(
            source.strip()
            for source in os.getenv("AITOS_STATISTICAL_DRIFT_SOURCES", "paper").split(
                ","
            )
            if source.strip()
        ),
    )
    scanner = next(
        (
            module
            for module in components.all_modules()
            if getattr(module, "module_id", None) == "opportunity-scanner"
        ),
        None,
    )
    if scanner is None:
        raise RuntimeError("opportunity-scanner unavailable for A-Stat drift lifecycle")

    registry = StatisticalModelRegistry(
        get_model_data_dir() / "statistical_model_registry.sqlite3"
    )
    governance = StatisticalGovernanceRuntime(registry)
    webhook_url = os.getenv("AITOS_STATISTICAL_DRIFT_WEBHOOK_URL", "").strip()
    webhook_token = os.getenv("AITOS_STATISTICAL_DRIFT_WEBHOOK_TOKEN", "").strip()
    if bool(webhook_url) != bool(webhook_token):
        raise RuntimeError(
            "both AITOS_STATISTICAL_DRIFT_WEBHOOK_URL and "
            "AITOS_STATISTICAL_DRIFT_WEBHOOK_TOKEN must be configured together"
        )
    alert_sink = (
        WebhookDriftAlertSink(webhook_url, bearer_token=webhook_token)
        if webhook_url
        else LoggingDriftAlertSink()
    )
    interval_seconds = float(
        os.getenv("AITOS_STATISTICAL_DRIFT_INTERVAL_SECONDS", "300")
    )
    engine = scanner._a_stat_bridge.engine
    await engine.start_drift_monitor(
        governance,
        provider,
        interval_seconds=interval_seconds,
        alert_callback=alert_sink,
    )
    cleanup.push_async_callback(engine.stop_drift_monitor)
    logger.info(
        "A-Stat drift monitor started",
        extra={
            "aitos_extra": {
                "features": list(feature_names),
                "sources": list(provider.sources),
                "interval_seconds": interval_seconds,
                "min_observations": provider.min_observations,
                "alert_sink": "webhook" if webhook_url else "structured_log",
                "promotion_policy": "operator_approval_required",
            }
        },
    )
    return engine, client


async def _run_paper_trading(cleanup: AsyncExitStack) -> None:
    settings = get_settings()
    if settings.tuning.mode != "paper":
        raise RuntimeError(
            "Trading entrypoint/configuration mode mismatch: run_paper_trading.py requires AITOS_MODE=paper"
        )
    configure_logging(settings.log_level)
    redis_client = await connect_redis_with_retry(settings, logger=logger)
    cleanup.push_async_callback(redis_client.close)
    from aitos.eventbus.factory import create_event_bus

    event_bus = create_event_bus(
        {
            "backend": os.getenv("AITOS_EVENTBUS_BACKEND", "redis"),
            "runtime_mode": "paper",
        },
        redis_client=redis_client,
    )
    await event_bus.initialize({})
    event_bus_cleanup_owner = {"components": False}
    drift_engine = None
    cleanup.push_async_callback(
        _shutdown_event_bus_if_unowned, event_bus, event_bus_cleanup_owner
    )
    market_repo, journal_repo = await connect_clickhouse_repositories(
        settings, logger=logger
    )
    cleanup.push_async_callback(market_repo.shutdown)
    cleanup.push_async_callback(journal_repo.shutdown)
    graph_driver = await try_connect_neo4j(settings, logger=logger)
    if graph_driver is not None:
        cleanup.push_async_callback(graph_driver.close)
    exchange = BinanceFuturesAdapter()
    cleanup.push_async_callback(exchange.close)
    symbols = await resolve_live_universe(
        exchange,
        core_symbols=settings.tuning.scanner.core_symbols,
        min_quote_volume_usd=settings.tuning.scanner.min_quote_volume_usd,
        min_trade_count=settings.tuning.scanner.min_trade_count,
        min_contract_age_days=settings.tuning.scanner.min_contract_age_days,
        dynamic_limit=settings.tuning.scanner.dynamic_universe_limit,
        universe_limit=settings.tuning.scanner.universe_limit,
    )
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
    world_intelligence_provider = None
    if os.getenv("AITOS_WORLD_INTELLIGENCE_SHADOW_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        from aitos.world_intelligence.context import ClickHouseAssetSnapshotProvider
        from aitos.world_intelligence.storage import ClickHouseIntelligenceStore

        intelligence_store = ClickHouseIntelligenceStore(market_repo._client)
        await intelligence_store.ensure_schema()
        world_intelligence_provider = ClickHouseAssetSnapshotProvider(
            intelligence_store,
            lookback_hours=float(
                os.getenv("AITOS_WORLD_INTELLIGENCE_LOOKBACK_HOURS", "72")
            ),
            max_events=int(os.getenv("AITOS_WORLD_INTELLIGENCE_MAX_EVENTS", "5000")),
            cache_ttl_seconds=float(
                os.getenv("AITOS_WORLD_INTELLIGENCE_CACHE_TTL_SECONDS", "5")
            ),
        )
        logger.info("WIATS scanner evidence enabled in shadow-only mode")
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
        world_intelligence_provider=world_intelligence_provider,
    )
    event_bus_cleanup_owner["components"] = True
    cleanup.push_async_callback(shutdown_all, components)
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
    # Explicitly opted-in shadow mirror only: legacy recovery remains authoritative.
    from aitos.trading.paper_shadow_runtime import (
        MirroredPaperStateStore,
        configured_shadow,
    )

    shadow = configured_shadow(os.environ, getattr(market_repo, "_client", None))
    persistence_store = state_store
    if shadow is not None:
        cleanup.push_async_callback(shadow.stop)
        await shadow.start()
        persistence_store = MirroredPaperStateStore(state_store, shadow)
    trade_state_persistence = TradeStatePersistence(
        event_bus, components.trade_lifecycle, persistence_store
    )
    cleanup.push_async_callback(trade_state_persistence.shutdown)
    # Ensure runtime-state tables and event subscriptions exist before recovery.
    # Restoring before initialization could query a table that has not yet been
    # created on a fresh/recreated ClickHouse volume.
    await trade_state_persistence.initialize()
    # Persist every entry intent before submission and recover uncertain submits
    # by exchange client-order-id lookup after a process restart.
    if not isinstance(
        components.trade_lifecycle._order_executor, IdempotentOrderExecutor
    ):
        components.trade_lifecycle._order_executor = IdempotentOrderExecutor(
            components.trade_lifecycle._order_executor, state_store
        )
    order_recovery = (
        await components.trade_lifecycle._order_executor.reconcile_pending_orders()
    )
    logger.info(
        "durable order-intent reconciliation complete",
        extra={"aitos_extra": order_recovery},
    )
    if order_recovery["filled_unlinked"]:
        logger.critical(
            "paper startup blocked: exchange fills lack durable trade snapshots",
            extra={"aitos_extra": order_recovery},
        )
        raise RuntimeError(
            "unlinked exchange fills require operator reconciliation before new entries"
        )
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
    position_subscription_result = (
        await trade_state_persistence.resubscribe_restored_positions()
    )
    position_recovery_ready = restored_position_feeds_ready(
        restored_count, position_subscription_result
    )
    if not position_recovery_ready:
        logger.critical(
            "paper startup blocked: restored positions lack verified market-data feeds",
            extra={
                "aitos_extra": {
                    "restored_open_trades": restored_count,
                    "subscription_result": position_subscription_result,
                    "new_entries_allowed": False,
                }
            },
        )

    # Register AI Kernel agents for multi-agent consensus
    market_agent = MarketAgent(event_bus=event_bus)
    risk_agent = RiskAgent(event_bus=event_bus)
    portfolio_agent = PortfolioAgent(event_bus=event_bus)
    learning_agent = LearningAgent(event_bus=event_bus)

    agents = (market_agent, risk_agent, portfolio_agent, learning_agent)
    for agent in agents:
        cleanup.push_async_callback(agent.shutdown)
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
    cleanup.push_async_callback(market_os_persistence.shutdown)
    await market_os_persistence.initialize({})
    experience_recorder = LearningExperienceRecorder(
        event_bus, market_repo, source="paper"
    )
    cleanup.push_async_callback(experience_recorder.shutdown)
    await experience_recorder.initialize({})
    await _start_optional_hawkes_bootstrap(components)
    drift_engine, _ = await _start_optional_statistical_drift_monitor(
        components, market_repo, settings, cleanup
    )
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
        console_token=os.getenv("AITOS_CONSOLE_TOKEN"),
        console_state_store=state_store,
        console_runtime_mode="paper",
        console_projection_observer=shadow,
    )
    cleanup.push_async_callback(health_server.stop)
    await health_server.start()

    stop_event = asyncio.Event()

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
            await restart_unhealthy_modules(auxiliary_modules)

    agents = (market_agent, risk_agent, portfolio_agent, learning_agent)
    auxiliary_modules = [
        experience_recorder,
        market_os_persistence,
        *agents,
    ]

    heal_task = asyncio.create_task(_self_heal_loop())
    cleanup.push_async_callback(_cancel_task, heal_task)
    from aitos.config.settings import get_settings as _get_settings
    from aitos.intelligence.position_runtime import get_tracked_lifecycles
    from aitos.trading.price_safety_net import PositionPriceSafetyNet

    _settings = _get_settings()
    price_safety_net = PositionPriceSafetyNet(
        exchange,
        get_tracked_lifecycles,
        stale_symbol_blacklist=_settings.stale_symbol_blacklist,
    )
    cleanup.push_async_callback(price_safety_net.stop)
    price_safety_net.start()
    tracker = PaperPortfolioTracker(
        starting_equity_usd=STARTING_EQUITY_USD, state_store=state_store
    )
    await tracker.restore()
    logger.info(
        "paper portfolio ledger restored",
        extra={
            "aitos_extra": {
                "starting_equity_usd": STARTING_EQUITY_USD,
                "peak_equity_usd": tracker._peak_equity_usd,
                "closed_trades_restored": len(
                    components.trade_lifecycle.get_closed_trades()
                ),
            }
        },
    )
    if not position_recovery_ready:
        # Enter the managed finally path without running a scan cycle. This
        # shuts down already-started workers/modules while preventing new risk.
        stop_event.set()
    install_signal_handlers(stop_event)
    # Construct and start the blocking worker only after all other startup
    # wiring succeeds. This keeps its ClickHouse client and singleton lock
    # inside the managed main try/finally lifetime as well as its thread.
    learning_worker = ContinualLearningWorker(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        user=settings.clickhouse.user,
        password=settings.clickhouse.password,
        database=settings.clickhouse.database,
    )
    learning_task = asyncio.create_task(asyncio.to_thread(learning_worker.run_forever))
    learning_task.set_name("continual-learning-worker")
    cleanup.push_async_callback(_stop_learning_worker, learning_worker, learning_task)
    # Startup succeeded; the existing runtime finally block now owns teardown.
    # If any earlier startup step raises, AsyncExitStack closes acquired resources.
    cleanup.pop_all()
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
                await tracker.persist_state(components.trade_lifecycle)
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
        try:
            if drift_engine is not None:
                try:
                    await drift_engine.stop_drift_monitor()
                except Exception:
                    logger.exception("A-Stat drift monitor shutdown failed")
            await _cancel_task(heal_task)
            # Let the worker finish its current ClickHouse batch before joining.
            await _stop_learning_worker(learning_worker, learning_task)
            await price_safety_net.stop()
            await _shutdown_agents(agents)
            await tracker.persist_state(components.trade_lifecycle)
            await asyncio.to_thread(rl_scorer.save_state)
            await asyncio.to_thread(outcome_classifier.save_state)
            await asyncio.to_thread(
                save_attention_model, attention_explainer, attention_path
            )
            await health_server.stop()
            await experience_recorder.shutdown()
            await market_os_persistence.shutdown()
            await trade_state_persistence.shutdown()
            if shadow is not None:
                await shadow.stop()
            await shutdown_all(components)
            await market_repo.shutdown()
            await journal_repo.shutdown()
            if graph_driver is not None:
                await graph_driver.close()
            await redis_client.close()

        finally:
            # Also drain the new worker if an earlier legacy teardown step fails.
            if shadow is not None:
                await shadow.stop()
        logger.info(
            "paper runtime shutdown complete",
            extra={"aitos_extra": {"shutdown_status": "complete"}},
        )


async def main() -> None:
    async with AsyncExitStack() as cleanup:
        await _run_paper_trading(cleanup)


if __name__ == "__main__":
    asyncio.run(main())
