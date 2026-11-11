#!/usr/bin/env python3
"""AITOS LIVE trading entrypoint with durable production runtime state."""

from __future__ import annotations

import asyncio
import os
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
from aitos.execution.position_integrity import assert_position_snapshot_matches
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
    approved_by = confirm_live_trading(
        symbols,
        testnet=settings.binance.testnet,
    )
    raw_order_executor = await prepare_live_executor(settings, symbols)
    cleanup.push_async_callback(raw_order_executor.close)
    order_executor = IdempotentOrderExecutor(raw_order_executor)
    state_store = DurableTradingStateStore(market_repo)
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
            require_human_approval_for_prod=True,
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
    await initialize_all(components)
    # First consume already-filled known protective orders, then compare the
    # resulting local ledger against the entire account before any new entry.
    if components.reconciliation is not None:
        await components.reconciliation.run_once()
    assert_position_snapshot_matches(
        components.trade_lifecycle.get_open_trades(),
        await raw_order_executor.get_position_risk(),
        hedge_mode=raw_order_executor.hedge_mode,
    )
    gateway = components.trade_lifecycle._aitos_capital_gateway
    restored_trades = components.trade_lifecycle.get_open_trades()
    active_symbols = {trade.symbol for trade in restored_trades}
    # A crash after durable close but before release may leave a flat lease.
    # Only the matched full-account snapshot and this stable owner permit release.
    for reservation in gateway.reservation.snapshot():
        if reservation.symbol not in active_symbols:
            gateway.release(reservation.symbol)
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
    )
    cleanup.push_async_callback(income_reconciler.shutdown)
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
    if not position_recovery_ready:
        # Fail closed through the managed shutdown path; never scan or submit
        # orders while a restored position is outside verified feed coverage.
        stop_event.set()
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
            if components.reconciliation is not None:
                await components.reconciliation.run_once()
            await trade_state_persistence.verify_execution_positions()
            submitted = await run_scan_and_trade_cycle(
                components,
                tracker,
                is_production=True,
                approved_by=approved_by,
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
        except TimeoutError:
            pass


async def main() -> None:
    async with AsyncExitStack() as cleanup:
        await _run_live_trading(cleanup)


if __name__ == "__main__":
    asyncio.run(main())
