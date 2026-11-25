#!/usr/bin/env python3
"""AITOS LIVE trading entrypoint with durable production runtime state."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import AsyncExitStack

from aitos.app import (
    build_system,
    initialize_all,
    run_scan_and_trade_cycle,
    shutdown_all,
)
from aitos.config.paths import get_model_data_dir
from aitos.config.settings import get_settings
from aitos.data.market_os_persistence import MarketOSPersistence
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.exchange.cost_adjustments import BinanceIncomeReconciliationService
from aitos.exchange.symbol_filter_refresher import SymbolFilterRefresher
from aitos.execution.postgres_oms import PostgresCanonicalOMS
from aitos.execution.postgres_intents import PostgresCanonicalExecutionIntentStore
from aitos.execution.postgres_outbox_projector import PostgresOMSOutboxProjector
from aitos.execution.protection_reconciliation import (
    recover_accepted_initial_stop_submissions,
    recover_protection_handovers,
    recover_triggered_protection_child_fills,
    require_live_account_exposure_reconciled,
)
from aitos.health_server import HealthServer
from aitos.intelligence.capital_protection import Reservation
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.kernel.ai_kernel import AIKernel
from aitos.learning.recorder import LearningExperienceRecorder
from aitos.live_trading import (
    confirm_live_trading,
    create_live_capital_gateway,
    prepare_live_executor,
)
from aitos.logging_setup import configure_logging, get_logger
from aitos.market_data.universe import resolve_live_universe
from aitos.runtime_connections import (
    connect_clickhouse_repositories,
    connect_redis_with_retry,
    try_connect_neo4j,
)
from aitos.runtime_ownership import RuntimeOwnership
from aitos.runtime_signals import install_signal_handlers
from aitos.storage.postgres_migrations import (
    apply_postgres_migrations,
    verify_postgres_schema,
)
from aitos.trading.persistent_state import (
    DurableTradingStateStore,
    IdempotentOrderExecutor,
    PersistentLivePortfolioTracker,
    TradeStatePersistence,
    restored_position_feeds_ready,
)
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.persistence import load_attention_model, save_attention_model

logger = get_logger("aitos.run_live_trading")
SCAN_INTERVAL_SECONDS = get_settings().tuning.scanner.refresh_interval_seconds
KLINE_TIMEFRAME = "15m"
HEALTH_SERVER_PORT = 8091
SYMBOL_FILTER_TTL_SECONDS = 300.0


async def _shutdown_event_bus_if_unowned(event_bus, ownership) -> None:
    if not ownership["components"]:
        await event_bus.shutdown()


async def _run_live_trading(cleanup: AsyncExitStack) -> None:
    settings = get_settings()
    if settings.tuning.mode != "live":
        raise RuntimeError(
            "Trading entrypoint/configuration mode mismatch: run_live_trading.py requires AITOS_MODE=live"
        )
    configure_logging(settings.log_level)
    ownership = RuntimeOwnership(get_model_data_dir() / "live_runtime.owner.lock")
    ownership.acquire()
    cleanup.callback(ownership.release)
    redis_client = await connect_redis_with_retry(settings, logger=logger)
    cleanup.push_async_callback(redis_client.aclose)
    from aitos.eventbus.factory import create_event_bus

    event_bus = create_event_bus(
        {
            "backend": os.getenv("AITOS_EVENTBUS_BACKEND", "redis"),
            "runtime_mode": "live",
        },
        redis_client=redis_client,
    )
    await event_bus.initialize({})
    event_bus_cleanup_owner = {"components": False}
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
    exchange = BinanceFuturesAdapter(
        environment="testnet" if settings.binance.testnet else "mainnet"
    )
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
    approved_by = confirm_live_trading(
        symbols,
        testnet=settings.binance.testnet,
    )
    raw_order_executor = await prepare_live_executor(settings, symbols)
    cleanup.push_async_callback(raw_order_executor.close)
    postgres_dsn = os.getenv("DATABASE_URL", "").strip()
    if not postgres_dsn:
        raise RuntimeError(
            "live trading requires DATABASE_URL for the canonical PostgreSQL OMS"
        )
    import asyncpg

    postgres_pool = await asyncpg.create_pool(
        dsn=postgres_dsn,
        min_size=1,
        max_size=5,
        command_timeout=10,
    )
    cleanup.push_async_callback(postgres_pool.close)
    await apply_postgres_migrations(postgres_pool)
    await verify_postgres_schema(postgres_pool)
    postgres_oms = PostgresCanonicalOMS(postgres_pool)
    writer_id = f"live:{socket.gethostname()}:{os.getpid()}"
    current_generation = await postgres_oms.writer_generation(
        raw_order_executor.execution_scope
    )
    writer_fence = await postgres_oms.acquire_writer(
        raw_order_executor.execution_scope,
        writer_id,
        expected_generation=current_generation,
    )
    canonical_intents = PostgresCanonicalExecutionIntentStore(
        postgres_oms, writer_fence
    )
    raw_order_executor.bind_postgres_intent_store(canonical_intents)
    intent_recovery = await raw_order_executor.refresh_canonical_intent_cache()
    if intent_recovery["unresolved"]:
        logger.critical(
            "unresolved PostgreSQL execution intents found at startup; "
            "fresh exposure remains blocked pending venue/trade reconciliation",
            extra={"aitos_extra": intent_recovery},
        )
    order_executor = IdempotentOrderExecutor(raw_order_executor)
    state_store = DurableTradingStateStore(
        market_repo, account_scope=raw_order_executor.execution_scope
    )
    await state_store.initialize()
    trade_state_persistence = None
    rl_scorer = DeepValueRLScorer()
    rl_scorer.load_state()
    outcome_classifier = TradeOutcomeClassifier()
    outcome_classifier.load_state()
    attention_path = str(get_model_data_dir() / "online_ml" / "attention_explainer.pkl")
    attention_explainer = load_attention_model(attention_path) or AttentionExplainer()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=order_executor,
        symbols=symbols,
        kline_timeframe=KLINE_TIMEFRAME,
        scanner_timeframe=KLINE_TIMEFRAME,
        market_data_repository=market_repo,
        journal_repository=journal_repo,
        graph_driver=graph_driver,
        kernel=AIKernel(
            event_bus=event_bus,
            require_human_approval_for_prod=settings.require_human_approval_for_prod,
        ),
        rl_scorer=rl_scorer,
        outcome_classifier=outcome_classifier,
        attention_explainer=attention_explainer,
        use_exchange_side_stops=True,
    )
    event_bus_cleanup_owner["components"] = True
    cleanup.push_async_callback(shutdown_all, components)
    components.trade_lifecycle._aitos_capital_gateway = create_live_capital_gateway(
        raw_order_executor.execution_scope
    )
    logger.info(
        "resolved dynamic live-trading universe",
        extra={"aitos_extra": {"symbol_count": len(symbols)}},
    )
    trade_state_persistence = TradeStatePersistence(
        event_bus,
        components.trade_lifecycle,
        state_store,
    )
    cleanup.push_async_callback(trade_state_persistence.shutdown)
    restored_count = await trade_state_persistence.restore()
    await trade_state_persistence.initialize()
    components.trade_lifecycle.bind_postgres_canonical_oms(postgres_oms, writer_fence)
    canonical_writer = components.trade_lifecycle._postgres_canonical_oms_writer
    outbox_projector = PostgresOMSOutboxProjector(
        postgres_oms,
        canonical_writer,
        worker_id=writer_id,
        batch_size=100,
        lease_seconds=60,
    )
    # Replay projections left pending by a crash or a previous projection
    # failure before account reconciliation is allowed to authorize entries.
    startup_projection = await outbox_projector.run_once()
    if startup_projection["failed"]:
        raise RuntimeError(
            "canonical OMS projection recovery failed; live startup remains blocked"
        )
    canonical_restore = await canonical_writer.restore_lifecycle(
        components.trade_lifecycle
    )
    restored_count = canonical_restore["open"]
    logger.warning(
        "canonical PostgreSQL OMS restored lifecycle state",
        extra={"aitos_extra": canonical_restore},
    )
    await initialize_all(components)
    # First consume already-filled known protective orders, then compare the
    # resulting local ledger against the entire account before any new entry.
    if components.reconciliation is not None:
        await components.reconciliation.run_once()
    protection_recovery = await recover_accepted_initial_stop_submissions(
        raw_order_executor,
        raw_order_executor.execution_intent_store,
        components.trade_lifecycle.get_open_trades(),
        canonical_writer,
    )
    if protection_recovery["recovered"]:
        logger.warning(
            "recovered accepted protective stops from durable preclaims",
            extra={
                "aitos_extra": {
                    "recovered": protection_recovery["recovered"],
                    "client_ids": protection_recovery["recovered_client_ids"],
                }
            },
        )
    if protection_recovery["blocked"]:
        logger.error(
            "protective-order preclaims remain quarantined after startup recovery",
            extra={"aitos_extra": {"blocked": protection_recovery["blocked"]}},
        )
    handover_recovery = await recover_protection_handovers(
        raw_order_executor,
        raw_order_executor.execution_intent_store,
        components.trade_lifecycle.get_open_trades(),
        canonical_writer,
    )
    if handover_recovery["recovered"]:
        logger.warning(
            "recovered interrupted protective-order handovers",
            extra={
                "aitos_extra": {
                    "recovered": handover_recovery["recovered"],
                    "handover_ids": handover_recovery["recovered_handover_ids"],
                }
            },
        )
    if handover_recovery["blocked"]:
        logger.error(
            "protective-order handovers remain quarantined after startup recovery",
            extra={"aitos_extra": {"blocked": handover_recovery["blocked"]}},
        )
    child_fill_recovery = await recover_triggered_protection_child_fills(
        raw_order_executor,
        raw_order_executor.execution_intent_store,
        components.trade_lifecycle,
    )
    if child_fill_recovery["applied"]:
        logger.warning(
            "applied authenticated triggered protective child fills",
            extra={"aitos_extra": child_fill_recovery},
        )
    if child_fill_recovery["blocked"]:
        logger.critical(
            "triggered protective child fills remain quarantined",
            extra={"aitos_extra": {"blocked": child_fill_recovery["blocked"]}},
        )
    child_reconciliation_ready = not child_fill_recovery["blocked"] and all(
        item.get("terminal") is True for item in child_fill_recovery["applied"]
    )
    account_reconciliation_ready = child_reconciliation_ready
    try:
        await require_live_account_exposure_reconciled(
            raw_order_executor,
            raw_order_executor.execution_intent_store,
            components.trade_lifecycle.get_open_trades(),
        )
    except Exception as exc:  # noqa: BLE001
        account_reconciliation_ready = False
        logger.critical(
            "live startup entering manage-only mode: "
            "account exposure is not reconciled",
            extra={
                "aitos_extra": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "new_entries_allowed": False,
                }
            },
        )
    components.trade_lifecycle._account_reconciliation_error = (
        not account_reconciliation_ready
    )
    gateway = components.trade_lifecycle._aitos_capital_gateway
    restored_trades = components.trade_lifecycle.get_open_trades()
    active_symbols = {trade.symbol for trade in restored_trades}
    # A crash after durable close but before release may leave a flat lease.
    # Only the matched full-account snapshot and this stable owner permit release.
    if account_reconciliation_ready:
        for reservation in gateway.reservation.snapshot():
            if reservation.symbol not in active_symbols:
                gateway.release(reservation.symbol)
    if account_reconciliation_ready:
        for trade in restored_trades:
            if trade.leverage <= 0:
                raise ValueError("restored exposure has invalid leverage")
            gateway.reservation.restore_confirmed_exposure(
                Reservation(
                    trade.symbol,
                    trade.position_size_usd / trade.leverage,
                    trade.risk_amount_usd,
                )
            )
        gateway.assert_reservations_cover(
            [
                Reservation(
                    trade.symbol,
                    trade.position_size_usd / trade.leverage,
                    trade.risk_amount_usd,
                )
                for trade in restored_trades
            ]
        )
    else:
        logger.critical(
            "capital reservations were not reconciled because "
            "venue exposure is unverified; "
            "runtime remains manage-only",
            extra={"aitos_extra": {"new_entries_allowed": False}},
        )
    position_subscription_result = (
        await trade_state_persistence.resubscribe_restored_positions()
    )
    position_recovery_ready = restored_position_feeds_ready(
        restored_count, position_subscription_result
    )
    if not position_recovery_ready:
        logger.critical(
            "live startup blocked: restored positions lack verified market-data feeds",
            extra={
                "aitos_extra": {
                    "restored_open_trades": restored_count,
                    "subscription_result": position_subscription_result,
                    "new_entries_allowed": False,
                }
            },
        )
    income_reconciler = BinanceIncomeReconciliationService(
        exchange,
        components.trade_lifecycle,
        event_bus,
        state_store,
        # Cost adjustments must first update the canonical trade snapshot,
        # then append the fee/funding journal event under the same OMS fence.
        persist_trade=canonical_writer,
        canonical_posting=canonical_writer.append_cost_adjustment,
    )
    cleanup.push_async_callback(income_reconciler.shutdown)
    income_reconciliation_ready = False
    try:
        income_result = await income_reconciler.reconcile_startup()
        income_reconciliation_ready = True
        logger.info(
            "startup Binance fee/funding reconciliation completed",
            extra={"aitos_extra": income_result},
        )
    except Exception as exc:  # noqa: BLE001
        # Do not allow fresh exposure when fees/funding cannot be attributed
        # or the venue history is unavailable. Existing positions remain
        # managed by the shutdown/recovery path; no journal/cursor is cleared.
        logger.critical(
            "live startup blocked: Binance fee/funding reconciliation incomplete",
            extra={
                "aitos_extra": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "new_entries_allowed": False,
                }
            },
        )
    pending_canonical_intents = await canonical_intents.unresolved()
    canonical_intents_ready = not pending_canonical_intents
    if not canonical_intents_ready:
        account_reconciliation_ready = False
        components.trade_lifecycle._account_reconciliation_error = True
        logger.critical(
            "live startup remains manage-only: PostgreSQL execution intents are unresolved",
            extra={
                "aitos_extra": {
                    "pending_intents": len(pending_canonical_intents),
                    "client_ids": [
                        str(row.get("client_id", "")) for row in pending_canonical_intents[:20]
                    ],
                    "new_entries_allowed": False,
                }
            },
        )

    market_os_persistence = MarketOSPersistence(event_bus, market_repo)
    cleanup.push_async_callback(market_os_persistence.shutdown)
    await market_os_persistence.initialize({})
    experience_recorder = LearningExperienceRecorder(
        event_bus,
        market_repo,
        source="live",
    )
    cleanup.push_async_callback(experience_recorder.shutdown)
    await experience_recorder.initialize({})
    filter_refresher = SymbolFilterRefresher(
        exchange,
        raw_order_executor,
        symbols,
        ttl_seconds=SYMBOL_FILTER_TTL_SECONDS,
    )
    cleanup.push_async_callback(filter_refresher.stop)
    await filter_refresher.start()
    health_server = HealthServer(
        components.all_modules()
        + [experience_recorder, market_os_persistence, income_reconciler],
        # The health endpoint is intentionally container/network reachable.
        # nosec B104 - binding all interfaces is required for the Docker health check.
        host="0.0.0.0",  # nosec B104
        port=HEALTH_SERVER_PORT,
        console_token=os.getenv("AITOS_CONSOLE_TOKEN"),
        console_state_store=state_store,
        console_runtime_mode="live",
    )
    cleanup.push_async_callback(health_server.stop)
    await health_server.start()
    from aitos.intelligence.position_runtime import get_tracked_lifecycles
    from aitos.trading.price_safety_net import PositionPriceSafetyNet

    _settings = get_settings()
    price_safety_net = PositionPriceSafetyNet(
        exchange,
        get_tracked_lifecycles,
        stale_symbol_blacklist=_settings.stale_symbol_blacklist,
    )
    cleanup.push_async_callback(price_safety_net.stop)
    price_safety_net.start()
    tracker = PersistentLivePortfolioTracker(order_executor, state_store)
    await tracker.restore()
    await income_reconciler.initialize({})
    stop_event = asyncio.Event()
    new_entries_allowed = (
        position_recovery_ready
        and income_reconciliation_ready
        and account_reconciliation_ready
    )
    if not new_entries_allowed:
        # Keep market ingestion, lifecycle price management, exchange protection
        # reconciliation and existing-position supervision alive, but do not
        # run a fresh-exposure scan until an operator restarts after remediation.
        logger.critical(
            "live runtime entering manage-only mode; fresh exposure is disabled",
            extra={
                "aitos_extra": {
                    "position_recovery_ready": position_recovery_ready,
                    "income_reconciliation_ready": income_reconciliation_ready,
                    "account_reconciliation_ready": account_reconciliation_ready,
                    "new_entries_allowed": False,
                }
            },
        )
    install_signal_handlers(stop_event)
    # One teardown owner on both startup failure and normal exit. Draining is
    # last registered so durable writers/executor remain available until done.
    cleanup.push_async_callback(asyncio.to_thread, rl_scorer.save_state)
    cleanup.push_async_callback(asyncio.to_thread, outcome_classifier.save_state)
    cleanup.push_async_callback(
        asyncio.to_thread, save_attention_model, attention_explainer, attention_path
    )
    cleanup.push_async_callback(components.trade_lifecycle.drain_live_submissions)
    while not stop_event.is_set():
        try:
            projection_result = await outbox_projector.run_once()
            if projection_result["failed"]:
                raise RuntimeError(
                    "canonical OMS projection replay failed; refusing fresh-exposure cycle"
                )
            if components.reconciliation is not None:
                await components.reconciliation.run_once()
            await trade_state_persistence.verify_execution_positions()
            # Reconcile full account positions, ordinary entry orders and
            # conditional protection before each fresh-exposure scan.
            await require_live_account_exposure_reconciled(
                raw_order_executor,
                raw_order_executor.execution_intent_store,
                components.trade_lifecycle.get_open_trades(),
            )
            if new_entries_allowed:
                submitted = await run_scan_and_trade_cycle(
                    components,
                    tracker,
                    is_production=True,
                    approved_by=approved_by,
                )
            else:
                # Market events continue to drive TradeLifecycle price/EIE
                # management; this loop still reconciles fills, positions and
                # protective orders, but never creates new exposure.
                submitted = 0
                logger.warning(
                    "manage-only cycle: fresh-exposure scan remains disabled",
                    extra={
                        "aitos_extra": {
                            "open_trades": len(
                                components.trade_lifecycle.get_open_trades()
                            ),
                            "new_entries_allowed": False,
                        }
                    },
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
                "live scan cycle complete",
                extra={
                    "aitos_extra": {
                        "submitted": submitted,
                        "open_trades": len(
                            components.trade_lifecycle.get_open_trades()
                        ),
                        "account_equity_usd": tracker._last_known_equity_usd,
                        "drawdown_peak_equity_usd": tracker._peak_equity_usd,
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
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=get_settings().tuning.scanner.refresh_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    async with AsyncExitStack() as cleanup:
        await _run_live_trading(cleanup)


if __name__ == "__main__":
    asyncio.run(main())
