"""System wiring for the AITOS trading application."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from aitos.config.paths import get_model_data_dir
from aitos.config.settings import get_settings
from aitos.config.tuning import effective_tuning_report, reload_tuning_settings
from aitos.core.contracts import AITOSModule, Event
from aitos.data.ingestion import DataIngestionService
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.contracts import EventBusProtocol, SubscriptionProtocol
from aitos.exchange.base import ExchangeAdapter
from aitos.execution.order_executor import OrderExecutor
from aitos.intelligence.rl_feedback import RLFeedbackLoop
from aitos.intelligence.rl_policy import RLPolicyScorer, TabularBanditRLScorer
from aitos.intelligence.scanner import OpportunityScanner
from aitos.statistics.feedback import AStatFeedbackLoop

# Keep model persistence independent of any developer-specific host path.
# MODEL_DATA_DIR is already part of the deployment environment contract;
# /models is the portable container default.
_MODEL_DATA_DIR = get_model_data_dir()
for _rl_dir in (_MODEL_DATA_DIR / "online_rl", _MODEL_DATA_DIR / "online_ml"):
    _rl_dir.mkdir(parents=True, exist_ok=True)

from aitos.journal.decision_repository import DecisionJournalRepository
from aitos.journal.journal_system import JournalSystem
from aitos.journal.performance_evaluator import DecisionPerformanceEvaluator
from aitos.journal.policy_monitor_service import PolicyMonitorService
from aitos.journal.repository import JournalRepository
from aitos.kernel.ai_kernel import AIKernel
from aitos.knowledge_graph.correlation_updater import SymbolCorrelationUpdater
from aitos.knowledge_graph.writer import GraphDriver, KnowledgeGraphWriter
from aitos.logging_setup import get_logger
from aitos.models.trade import Opportunity, TradeLifecycleState
from aitos.risk.models import PortfolioState, PositionExposure, RiskLimits
from aitos.risk.risk_engine import RiskEngine
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.position_manager import PositionManager
from aitos.trading.reconciliation import ReconciliationScheduler
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.attention_feedback import AttentionFeedbackLoop
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.ml_feedback import MLExplainerFeedbackLoop

PositionSwitchingFactory = Callable[
    [TradeLifecycle, PositionManager | None, ExchangeAdapter, OrderExecutor], Any
]

logger = get_logger("aitos.app")


@dataclass
class SystemComponents:
    event_bus: EventBusProtocol
    kernel: AIKernel
    risk_engine: RiskEngine
    data_ingestion: DataIngestionService
    scanner: OpportunityScanner
    trade_lifecycle: TradeLifecycle
    journal: JournalSystem
    decision_journal: DecisionJournalRepository
    performance_evaluator: DecisionPerformanceEvaluator
    policy_monitor: PolicyMonitorService
    rl_scorer: RLPolicyScorer
    rl_feedback: RLFeedbackLoop
    outcome_classifier: TradeOutcomeClassifier
    ml_feedback: MLExplainerFeedbackLoop
    attention_explainer: AttentionExplainer
    attention_feedback: AttentionFeedbackLoop
    a_stat_feedback: AStatFeedbackLoop
    reconciliation: ReconciliationScheduler | None = None
    knowledge_graph: KnowledgeGraphWriter | None = None
    correlation_updater: SymbolCorrelationUpdater | None = None
    _price_feed_subscriptions: list[SubscriptionProtocol] = field(default_factory=list)

    def reload_runtime_tuning(self) -> dict[str, object]:
        """Reload tunings that can be changed without rebuilding infrastructure.

        Universe subscriptions, Redis pool/consumer configuration and the hard
        max-open-position risk cap are restart-bound; reject those changes
        instead of silently applying only part of a new profile.
        """
        current = self.scanner._tuning
        updated = reload_tuning_settings()
        restart_required = (
            current.mode != updated.mode
            or current.event_bus != updated.event_bus
            or current.risk != updated.risk
            or current.hardware != updated.hardware
            or current.storage != updated.storage
            or current.telemetry != updated.telemetry
            or current.positions != updated.positions
        )
        if restart_required:
            raise ValueError(
                "Runtime reload changed mode, infrastructure, hardware, storage, "
                "telemetry, risk ceilings or position-management tuning; restart AITOS "
                "to apply this profile safely"
            )
        self.scanner.apply_tuning_settings(updated)
        self.trade_lifecycle.apply_tuning_settings(updated)
        report = effective_tuning_report(updated)
        logger.info(
            "AITOS runtime tuning reloaded",
            extra={
                "aitos_extra": {
                    "profile": report["profile"],
                    "mode": report["mode"],
                    "config_version": report["config_version"],
                    "restart_required": False,
                }
            },
        )
        return report

    def all_modules(self) -> list[AITOSModule]:
        modules: list[AITOSModule] = [
            self.event_bus,
            self.kernel,
            self.risk_engine,
            self.decision_journal,
            self.journal,
            self.performance_evaluator,
            self.policy_monitor,
            self.rl_feedback,
            self.ml_feedback,
            self.attention_feedback,
            self.a_stat_feedback,
        ]
        if self.knowledge_graph is not None:
            modules.append(self.knowledge_graph)
        modules += [self.scanner, self.data_ingestion, self.trade_lifecycle]
        if self.reconciliation is not None:
            modules.append(self.reconciliation)
        if self.correlation_updater is not None:
            modules.append(self.correlation_updater)
        return modules


async def build_system(
    event_bus: EventBusProtocol,
    exchange: ExchangeAdapter,
    order_executor: OrderExecutor,
    symbols: list[str],
    kline_timeframe: str = "15m",
    scanner_timeframe: str = "15m",
    market_data_repository: MarketDataRepository | None = None,
    journal_repository: JournalRepository | None = None,
    decision_journal_repository: DecisionJournalRepository | None = None,
    graph_driver: GraphDriver | None = None,
    risk_limits: RiskLimits | None = None,
    kernel: AIKernel | None = None,
    rl_scorer: RLPolicyScorer | None = None,
    outcome_classifier: TradeOutcomeClassifier | None = None,
    attention_explainer: AttentionExplainer | None = None,
    use_exchange_side_stops: bool = False,
    min_score_threshold: float = 60.0,
    top_n: int | None = None,
    enable_exit_intelligence: bool = False,
    position_switching_factory: PositionSwitchingFactory | None = None,
) -> SystemComponents:
    settings = get_settings()
    tuning = settings.tuning
    tuning_report = effective_tuning_report(tuning)
    logger.info(
        "AITOS effective runtime tuning",
        extra={
            "aitos_extra": {
                "profile": tuning_report["profile"],
                "mode": tuning_report["mode"],
                "config_version": tuning_report["config_version"],
                "scanner": tuning.scanner.model_dump(mode="json"),
                "analysis": tuning.analysis.model_dump(mode="json"),
                "event_bus": tuning.event_bus.model_dump(mode="json"),
                "positions": tuning.positions.model_dump(mode="json"),
                "risk": tuning.risk.model_dump(mode="json"),
                "hardware": tuning.hardware.model_dump(mode="json"),
                "storage": tuning.storage.model_dump(mode="json"),
                "telemetry": tuning.telemetry.model_dump(mode="json"),
            }
        },
    )
    if risk_limits is None:
        risk_limits = RiskLimits(
            max_open_positions=tuning.positions.max_open_positions,
            max_risk_per_trade_pct=tuning.risk.max_risk_per_trade_pct,
            max_leverage=tuning.risk.max_leverage,
            max_drawdown_pct=tuning.risk.max_drawdown_pct,
            max_correlated_exposure_pct=tuning.risk.max_correlated_exposure_pct,
            max_sector_exposure_pct=tuning.risk.max_sector_exposure_pct,
        )
    else:
        # Central tuning may tighten caller-supplied limits but can never relax
        # them. The schema itself also caps values at the established defaults.
        risk_limits = risk_limits.model_copy(
            update={
                "max_open_positions": min(
                    risk_limits.max_open_positions,
                    tuning.positions.max_open_positions,
                ),
                "max_risk_per_trade_pct": min(
                    risk_limits.max_risk_per_trade_pct,
                    tuning.risk.max_risk_per_trade_pct,
                ),
                "max_leverage": min(risk_limits.max_leverage, tuning.risk.max_leverage),
                "max_drawdown_pct": min(
                    risk_limits.max_drawdown_pct, tuning.risk.max_drawdown_pct
                ),
                "max_correlated_exposure_pct": min(
                    risk_limits.max_correlated_exposure_pct,
                    tuning.risk.max_correlated_exposure_pct,
                ),
                "max_sector_exposure_pct": min(
                    risk_limits.max_sector_exposure_pct,
                    tuning.risk.max_sector_exposure_pct,
                ),
            }
        )
    kernel = kernel or AIKernel(event_bus=event_bus)
    risk_engine = RiskEngine(event_bus=event_bus, limits=risk_limits)
    if rl_scorer is None:
        try:
            from aitos.intelligence.deep_rl_policy import DeepValueRLScorer

            rl_scorer = DeepValueRLScorer()
        except ImportError:
            rl_scorer = TabularBanditRLScorer()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=symbols,
        timeframe=scanner_timeframe,
        rl_scorer=rl_scorer,
        min_score_threshold=min_score_threshold,
        top_n=top_n,
        tuning=tuning,
    )
    rl_feedback = RLFeedbackLoop(event_bus=event_bus, scorer=rl_scorer)
    outcome_classifier = outcome_classifier or TradeOutcomeClassifier()
    ml_feedback = MLExplainerFeedbackLoop(
        event_bus=event_bus, classifier=outcome_classifier
    )
    attention_explainer = attention_explainer or AttentionExplainer()
    attention_feedback = AttentionFeedbackLoop(
        event_bus=event_bus, explainer=attention_explainer
    )
    a_stat_feedback = AStatFeedbackLoop(
        event_bus=event_bus,
        engine=scanner._a_stat_bridge.engine,
        database_path=_MODEL_DATA_DIR / "a_stat_feedback.sqlite3",
    )
    position_manager = (
        PositionManager(
            config={
                "adaptive_exit_policy": {
                    "stagnation_bars": tuning.positions.dead_candle_exit_bars,
                    "stagnation_min_progress_r": (
                        tuning.positions.dead_candle_max_progress_r
                    ),
                    "stagnation_entry_band_pct": (
                        tuning.positions.dead_candle_entry_band_pct
                    ),
                }
            }
        )
        if enable_exit_intelligence
        else None
    )
    if position_manager is not None:
        logger.info("Exit Intelligence (PositionManager) enabled for TradeLifecycle")
    trade_lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=order_executor,
        kernel=kernel,
        use_exchange_side_stops=use_exchange_side_stops,
        position_manager=position_manager,
    )
    data_ingestion = DataIngestionService(
        exchange=exchange,
        event_bus=event_bus,
        symbols=symbols,
        kline_timeframe=kline_timeframe,
        repository=market_data_repository,
        live_trade_handler=scanner.accept_live_trade,
        live_orderbook_handler=scanner.accept_live_order_book,
    )
    # Share the bounded structural-liquidity snapshot with the position context.
    scanner._aitos_liquidity_state_store = data_ingestion.live_state
    if position_manager is not None:
        from aitos.trading.market_context import LiveStateContextProvider

        provider = LiveStateContextProvider(data_ingestion.live_state)
        # Prefer native Phase-G property; fall back to private attr + wrap.
        if hasattr(type(trade_lifecycle), "market_context_provider"):
            trade_lifecycle.market_context_provider = provider  # type: ignore[attr-defined]
        else:
            object.__setattr__(trade_lifecycle, "_market_context_provider", provider)
            _orig_handle = trade_lifecycle.handle_event
            _orig_close = trade_lifecycle.close_trade

            async def _handle_with_context(event: Event):
                if event.topic.startswith("market.kline.") or event.topic.startswith(
                    "market.trade."
                ):
                    symbol = event.payload.get("symbol")
                    price = event.payload.get("close", event.payload.get("price"))
                    from aitos.trading.lifecycle import _valid_market_price

                    if symbol and _valid_market_price(price):
                        current_price = float(price)
                        try:
                            ctx_kwargs = provider.get_context(symbol).as_kwargs()
                        except Exception:
                            ctx_kwargs = {}
                        for trade in list(trade_lifecycle.get_open_trades()):
                            if trade.symbol == symbol:
                                await trade_lifecycle.update_price(
                                    trade.trade_id, current_price, **ctx_kwargs
                                )
                        return None
                return await _orig_handle(event)

            async def _close_with_clear(trade_id: str, exit_price: float, reason: str):
                trade = await _orig_close(trade_id, exit_price, reason)
                try:
                    position_manager.clear_trade(trade.trade_id, symbol=trade.symbol)
                except Exception:
                    pass
                return trade

            object.__setattr__(trade_lifecycle, "handle_event", _handle_with_context)
            object.__setattr__(trade_lifecycle, "close_trade", _close_with_clear)
        logger.info("LiveStateContextProvider wired into TradeLifecycle")
    if position_switching_factory is not None:
        coordinator = position_switching_factory(
            trade_lifecycle, position_manager, exchange, order_executor
        )
        if inspect.isawaitable(coordinator):
            coordinator = await coordinator
        trade_lifecycle.configure_position_switching(coordinator)
        logger.info("Durable position-switching coordinator explicitly configured")

    decision_journal = decision_journal_repository
    if (
        decision_journal is None
        and journal_repository is not None
        and all(
            hasattr(journal_repository, method)
            for method in (
                "save_decision",
                "link_trade",
                "attribute_outcome",
                "get_records",
                "save_management_decision",
                "get_trade_records",
            )
        )
    ):
        # The PostgreSQL fallback implements both operational journal
        # interfaces over one append-only transactional record table.
        decision_journal = journal_repository
    if decision_journal is None:
        decision_journal = DecisionJournalRepository()
    journal = JournalSystem(
        event_bus=event_bus,
        repository=journal_repository,
        risk_engine=risk_engine,
        decision_repository=decision_journal,
    )
    performance_evaluator = DecisionPerformanceEvaluator(decision_journal)
    policy_monitor = PolicyMonitorService(event_bus=event_bus)
    reconciliation = (
        ReconciliationScheduler(trade_lifecycle=trade_lifecycle, event_bus=event_bus)
        if order_executor.supports_exchange_side_stops and use_exchange_side_stops
        else None
    )
    knowledge_graph = correlation_updater = None
    if graph_driver is not None:
        knowledge_graph = KnowledgeGraphWriter(event_bus=event_bus, driver=graph_driver)
        correlation_updater = SymbolCorrelationUpdater(
            exchange=exchange,
            graph_writer=knowledge_graph,
            symbols=symbols,
            timeframe=scanner_timeframe,
        )
    return SystemComponents(
        event_bus=event_bus,
        kernel=kernel,
        risk_engine=risk_engine,
        data_ingestion=data_ingestion,
        scanner=scanner,
        trade_lifecycle=trade_lifecycle,
        journal=journal,
        decision_journal=decision_journal,
        performance_evaluator=performance_evaluator,
        policy_monitor=policy_monitor,
        rl_scorer=rl_scorer,
        rl_feedback=rl_feedback,
        outcome_classifier=outcome_classifier,
        ml_feedback=ml_feedback,
        attention_explainer=attention_explainer,
        attention_feedback=attention_feedback,
        a_stat_feedback=a_stat_feedback,
        reconciliation=reconciliation,
        knowledge_graph=knowledge_graph,
        correlation_updater=correlation_updater,
    )


async def _health_status(module: AITOSModule):
    result = module.health_check()
    if inspect.isawaitable(result):
        return await result
    if inspect.isasyncgen(result):
        async for status in result:
            return status
        raise RuntimeError(
            f"Module {module.module_id} returned an empty health-check stream"
        )
    return result


async def restart_unhealthy_modules(modules: list[AITOSModule]) -> list[str]:
    """Restart only the explicitly supplied auxiliary modules that report unhealthy.

    Shutdown must succeed before initialize is attempted. This avoids creating
    duplicate subscriptions when an auxiliary module cannot release its old
    resources cleanly.
    """
    restarted: list[str] = []
    for module in modules:
        try:
            health = await module.health_check()
            if health.status.value != "unhealthy":
                continue
            await module.shutdown()
            await module.initialize({})
            refreshed = await module.health_check()
            if refreshed.status.value in ("healthy", "degraded"):
                restarted.append(module.module_id)
                logger.info(
                    "auxiliary module recovered",
                    extra={"aitos_extra": {"module_id": module.module_id}},
                )
            else:
                logger.error(
                    "auxiliary module remains unhealthy after restart",
                    extra={"aitos_extra": {"module_id": module.module_id}},
                )
        except Exception as exc:
            logger.exception(
                "auxiliary module restart failed",
                extra={
                    "aitos_extra": {
                        "module_id": getattr(module, "module_id", "unknown"),
                        "error": str(exc),
                    }
                },
            )
    return restarted


async def initialize_all(components: SystemComponents, *, timeout: float = 5.0) -> None:
    for module in components.all_modules():
        await module.initialize({})
    # Register incrementally: if a later subscription fails, the runner's
    # managed shutdown must still own every successfully acquired handle.
    for topic, group in (
        ("market.kline.*", "trade-lifecycle-prices"),
        ("market.trade.*", "trade-lifecycle-prices"),
        ("trade.cost_adjustment", "trade-lifecycle-costs"),
    ):
        components._price_feed_subscriptions.append(
            await components.event_bus.subscribe(
                topic, components.trade_lifecycle.handle_event, group=group
            )
        )
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        statuses = [await _health_status(module) for module in components.all_modules()]
        if all(status.status.value in ("healthy", "degraded") for status in statuses):
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"Some modules failed to initialize within {timeout}s timeout")


async def shutdown_all(
    components: SystemComponents, grace_period_seconds: float = 30.0
) -> None:
    for sub in components._price_feed_subscriptions:
        sub.cancel()
    for module in reversed(components.all_modules()):
        try:
            await module.shutdown(grace_period_seconds)
        except Exception as exc:
            logger.error(
                "error shutting down module",
                extra={
                    "aitos_extra": {"module_id": module.module_id, "error": str(exc)}
                },
            )


class PortfolioTracker(Protocol):
    def build_portfolio_state(
        self, trade_lifecycle: TradeLifecycle
    ) -> PortfolioState: ...


@dataclass
class PaperPortfolioTracker:
    starting_equity_usd: float = 10_000.0
    state_store: Any | None = None
    asset: str = "PAPER"
    _peak_equity_usd: float = field(init=False)

    def __post_init__(self) -> None:
        self._peak_equity_usd = self.starting_equity_usd

    async def restore(self) -> None:
        """Restore the high-water mark before the first risk assessment."""
        if self.state_store is None:
            return
        peak = await self.state_store.load_peak_equity(self.asset)
        if peak is not None:
            self._peak_equity_usd = max(self.starting_equity_usd, float(peak))

    async def persist_state(self, trade_lifecycle: TradeLifecycle) -> None:
        """Persist equity and its high-water mark after each completed cycle."""
        if self.state_store is None:
            return
        closed = trade_lifecycle.get_closed_trades()
        open_trades = trade_lifecycle.get_open_trades()
        realized_pnl = sum(float(t.pnl or 0.0) for t in (*closed, *open_trades))
        equity = self.starting_equity_usd + realized_pnl
        self._peak_equity_usd = max(self._peak_equity_usd, equity)
        await self.state_store.save_drawdown(self.asset, equity, self._peak_equity_usd)

    def build_portfolio_state(self, trade_lifecycle: TradeLifecycle) -> PortfolioState:
        closed, open_trades = (
            trade_lifecycle.get_closed_trades(),
            trade_lifecycle.get_open_trades(),
        )
        realized_pnl = sum(t.pnl for t in (*closed, *open_trades) if t.pnl is not None)
        equity = self.starting_equity_usd + realized_pnl
        self._peak_equity_usd = max(self._peak_equity_usd, equity)
        day_ago, week_ago = datetime.now(timezone.utc) - timedelta(
            days=1
        ), datetime.now(timezone.utc) - timedelta(days=7)
        daily_pnl = sum(
            t.pnl
            for t in closed
            if t.pnl is not None and t.exit_time and _parse_iso(t.exit_time) >= day_ago
        )
        weekly_pnl = sum(
            t.pnl
            for t in closed
            if t.pnl is not None and t.exit_time and _parse_iso(t.exit_time) >= week_ago
        )
        positions = tuple(
            PositionExposure(
                symbol=t.symbol,
                notional_usd=t.position_size_usd,
                leverage=t.leverage,
                side=t.side.value,
                risk_amount_usd=abs(
                    t.entry_price - float(t.emergency_sl_price or t.sl_price)
                )
                * t.quantity,
            )
            for t in open_trades
        )
        regime_counts: dict[str, int] = {}
        for t in open_trades:
            regime_counts[t.regime] = regime_counts.get(t.regime, 0) + 1
        dominant_regime = (
            max(regime_counts, key=regime_counts.get) if regime_counts else "unknown"
        )
        return PortfolioState(
            equity_usd=equity,
            peak_equity_usd=self._peak_equity_usd,
            positions=positions,
            daily_pnl_pct=(daily_pnl / equity * 100) if equity else 0.0,
            weekly_pnl_pct=(weekly_pnl / equity * 100) if equity else 0.0,
            regime=dominant_regime,
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class LivePortfolioTracker:
    def __init__(self, order_executor, asset: str = "USDT"):
        self._order_executor, self._asset = order_executor, asset
        self._peak_equity_usd: float | None = None
        self._last_known_equity_usd = 0.0

    async def refresh_equity(self) -> float:
        equity = await self._order_executor.get_account_balance(self._asset)
        self._last_known_equity_usd = equity
        self._peak_equity_usd = (
            equity
            if self._peak_equity_usd is None
            else max(self._peak_equity_usd, equity)
        )
        return equity

    def build_portfolio_state(self, trade_lifecycle: TradeLifecycle) -> PortfolioState:
        open_trades = trade_lifecycle.get_open_trades()
        positions = tuple(
            PositionExposure(
                symbol=t.symbol, notional_usd=t.position_size_usd, leverage=t.leverage
            )
            for t in open_trades
        )
        regime_counts: dict[str, int] = {}
        for t in open_trades:
            regime_counts[t.regime] = regime_counts.get(t.regime, 0) + 1
        dominant_regime = (
            max(regime_counts, key=regime_counts.get) if regime_counts else "unknown"
        )
        return PortfolioState(
            equity_usd=self._last_known_equity_usd,
            peak_equity_usd=self._peak_equity_usd or self._last_known_equity_usd,
            positions=positions,
            regime=dominant_regime,
        )


async def run_scan_and_trade_cycle(
    components: SystemComponents,
    portfolio_tracker: PortfolioTracker,
    is_production: bool = False,
    approved_by: str | None = None,
) -> int:
    if components.scanner._tuning.runtime.config_reload_enabled:
        try:
            components.reload_runtime_tuning()
        except ValueError as exc:
            # Keep the last known-good snapshot active. Changes requiring
            # infrastructure rebuild are reported and applied after restart.
            logger.error(
                "runtime tuning reload rejected; retaining current snapshot: %s",
                exc,
            )
    refresh = getattr(portfolio_tracker, "refresh_equity", None)
    if refresh is not None:
        await refresh()
    portfolio = portfolio_tracker.build_portfolio_state(components.trade_lifecycle)
    await components.risk_engine.assess(portfolio)
    open_symbol_counts: dict[str, int] = {}
    for open_trade in components.trade_lifecycle.get_open_trades():
        open_symbol_counts[open_trade.symbol] = (
            open_symbol_counts.get(open_trade.symbol, 0) + 1
        )
    max_per_symbol = get_settings().tuning.positions.max_positions_per_symbol
    submitted = 0
    accepted_opportunities = 0
    target_opportunities = components.scanner.target_opportunities
    stop_cycle = False

    ranked_batches = [
        batch async for batch in components.scanner.iter_candidate_batches()
    ]
    prevalidated: dict[int, tuple[Any, Opportunity]] = {}
    rebalance_opportunities: list[Opportunity] = []
    for batch in ranked_batches:
        for candidate in batch:
            if open_symbol_counts.get(candidate.symbol, 0) >= max_per_symbol:
                components.scanner.record_candidate_rejection(
                    candidate, "max_positions_per_symbol"
                )
                continue
            kernel_decision = await components.scanner.decide_with_kernel(
                candidate, components.kernel
            )
            if (
                kernel_decision.direction != candidate.direction.value.lower()
                or kernel_decision.confidence < components.kernel.fusion_min_confidence
            ):
                components.scanner.record_candidate_rejection(
                    candidate, "kernel_rejection"
                )
                prevalidated[id(candidate)] = (None, None)
                continue
            opportunity = components.scanner.to_opportunity(
                candidate, is_production=is_production, approved_by=approved_by
            )
            decision_id = opportunity.opportunity_id
            opportunity = replace(
                opportunity,
                confidence=min(opportunity.confidence, kernel_decision.confidence),
                rationale=f"kernel_confidence={kernel_decision.confidence:.4f}; "
                + opportunity.rationale,
                agent_consensus={
                    **opportunity.agent_consensus,
                    "kernel_fusion_confidence": kernel_decision.confidence,
                    "decision_id": decision_id,
                },
            )
            prevalidated[id(candidate)] = (kernel_decision, opportunity)
            rebalance_opportunities.append(opportunity)

    switch_attempted_ids: set[str] = set()
    if not is_production and rebalance_opportunities:
        switch_rankings = (
            await components.trade_lifecycle.evaluate_rebalance_scan_cycle(
                rebalance_opportunities
            )
        )
        if switch_rankings:
            incumbent, replacement, rebalance_decision = switch_rankings[0]
            switch_attempted_ids.add(replacement.opportunity_id)
            try:
                switched_trade = await components.trade_lifecycle.switch_position(
                    incumbent, replacement, rebalance_decision
                )
                if (
                    switched_trade is not None
                    and switched_trade.state == TradeLifecycleState.POSITION_OPENED
                ):
                    submitted += 1
                    accepted_opportunities += 1
                    components.scanner.clear_candidate_cooldown(replacement.symbol)
                    open_symbol_counts[incumbent.symbol] = max(
                        0, open_symbol_counts.get(incumbent.symbol, 0) - 1
                    )
                    open_symbol_counts[replacement.symbol] = (
                        open_symbol_counts.get(replacement.symbol, 0) + 1
                    )
            except Exception:
                logger.exception(
                    "safe position switch did not complete; replacement will not be "
                    "submitted again in this scan cycle",
                    extra={
                        "aitos_extra": {
                            "incumbent_trade_id": incumbent.trade_id,
                            "incumbent_symbol": incumbent.symbol,
                            "candidate_symbol": replacement.symbol,
                            "switch_id": (
                                f"switch:{incumbent.trade_id}:{replacement.opportunity_id}"
                            ),
                        }
                    },
                )
                rejected_candidate = next(
                    (
                        item
                        for batch in ranked_batches
                        for item in batch
                        if item.symbol == replacement.symbol
                    ),
                    None,
                )
                if rejected_candidate is not None:
                    components.scanner.record_candidate_rejection(
                        rejected_candidate, "rebalance_switch_requires_reconciliation"
                    )

    for ranked_batch in ranked_batches:
        for candidate in ranked_batch:
            if accepted_opportunities >= target_opportunities:
                components.scanner.record_candidate_rejection(
                    candidate, "opportunity_target_reached"
                )
                continue
            logger.info(
                "scanner candidate ranked",
                extra={
                    "aitos_extra": {
                        "symbol": candidate.symbol,
                        "direction": candidate.direction.value,
                        "score": candidate.composite_score,
                        "component_scores": dict(candidate.component_scores),
                        "weights": dict(components.scanner._weights),
                        "threshold": components.scanner._min_score_threshold,
                    }
                },
            )
            if open_symbol_counts.get(candidate.symbol, 0) >= max_per_symbol:
                logger.info(
                    "trade candidate skipped",
                    extra={
                        "aitos_extra": {
                            "symbol": candidate.symbol,
                            "reason": "max_positions_per_symbol_reached",
                            "max_positions_per_symbol": max_per_symbol,
                            "score": candidate.composite_score,
                        }
                    },
                )
                components.scanner.record_candidate_rejection(
                    candidate, "max_positions_per_symbol_after_rebalance"
                )
                continue

            decision, opportunity = prevalidated.get(id(candidate), (None, None))
            if decision is None or opportunity is None:
                continue
            if opportunity.opportunity_id in switch_attempted_ids:
                continue
            decision_id = opportunity.opportunity_id
            await components.event_bus.publish(
                Event(
                    topic="decision.snapshot",
                    payload={
                        "decision_id": decision_id,
                        "symbol": opportunity.symbol,
                        "side": opportunity.side.value,
                        "entry_price": opportunity.entry_price,
                        "stop_loss_price": opportunity.stop_loss_price,
                        "take_profit_levels": list(opportunity.take_profit_levels),
                        "confidence": opportunity.confidence,
                        "strategy_id": opportunity.strategy_id,
                        "rationale": opportunity.rationale,
                        "agent_consensus": dict(opportunity.agent_consensus),
                        "regime": opportunity.regime,
                        "detected_at": opportunity.detected_at,
                        "is_production": opportunity.is_production,
                        "approved_by": opportunity.approved_by,
                        "kernel_direction": decision.direction,
                        "kernel_confidence": decision.confidence,
                        "candidate_score": candidate.composite_score,
                        "candidate_component_scores": dict(candidate.component_scores),
                        "candidate_component_availability": dict(
                            candidate.component_availability or {}
                        ),
                        "candidate_weights": dict(components.scanner._weights),
                        "candidate_threshold": components.scanner._min_score_threshold,
                        "candidate_rationale": list(candidate.rationale or []),
                    },
                    source_module="aitos.app",
                )
            )
            trade = await components.trade_lifecycle.submit_opportunity(
                opportunity,
                portfolio_tracker.build_portfolio_state(components.trade_lifecycle),
            )
            submitted += 1
            rejection_reason = str(getattr(trade, "rejection_reason", "") or "").lower()
            portfolio_rejection = any(
                marker in rejection_reason
                for marker in (
                    "circuit breaker",
                    "drawdown",
                    "daily loss",
                    "hard limit",
                    "risk limit",
                    "max open positions",
                    "correlated exposure",
                    "sector exposure",
                    "insufficient capital",
                )
            )
            if trade.state in {
                TradeLifecycleState.ORDER_SUBMITTED,
                TradeLifecycleState.POSITION_OPENED,
            }:
                accepted_opportunities += 1
                components.scanner.clear_candidate_cooldown(candidate.symbol)
            elif portfolio_rejection:
                components.scanner.record_candidate_rejection(
                    candidate, "portfolio_risk_rejection"
                )
            else:
                components.scanner.record_candidate_rejection(
                    candidate, "lifecycle_rejection"
                )
            logger.info(
                "candidate lifecycle decision",
                extra={
                    "aitos_extra": {
                        "symbol": candidate.symbol,
                        "score": candidate.composite_score,
                        "kernel_confidence": decision.confidence,
                        "trade_state": trade.state.value,
                        "accepted_opportunities": accepted_opportunities,
                        "target_opportunities": target_opportunities,
                    }
                },
            )
            if trade.state == TradeLifecycleState.POSITION_OPENED:
                open_symbol_counts[candidate.symbol] = (
                    open_symbol_counts.get(candidate.symbol, 0) + 1
                )
            if trade.state == TradeLifecycleState.REJECTED and portfolio_rejection:
                logger.warning(
                    "candidate refill halted by portfolio risk rejection",
                    extra={
                        "aitos_extra": {
                            "symbol": candidate.symbol,
                            "rejection_reason": rejection_reason,
                        }
                    },
                )
                stop_cycle = True
                break
            if accepted_opportunities >= target_opportunities:
                break
        if stop_cycle or accepted_opportunities >= target_opportunities:
            break

    logger.info(
        "candidate rotation cycle completed",
        extra={
            "aitos_extra": {
                "submitted_attempts": submitted,
                "accepted_opportunities": accepted_opportunities,
                "target_opportunities": target_opportunities,
                "rotation": components.scanner._candidate_rotation.snapshot(),
                "portfolio_risk_halt": stop_cycle,
            }
        },
    )
    return submitted
