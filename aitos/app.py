"""System wiring for the AITOS trading application."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol

from aitos.core.contracts import AITOSModule, Event
from aitos.data.ingestion import DataIngestionService
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.exchange.base import ExchangeAdapter
from aitos.execution.order_executor import OrderExecutor
from aitos.intelligence.rl_feedback import RLFeedbackLoop
from aitos.intelligence.rl_policy import RLPolicyScorer, TabularBanditRLScorer
from aitos.intelligence.scanner import OpportunityScanner
from aitos.journal.decision_repository import DecisionJournalRepository
from aitos.journal.journal_system import JournalSystem
from aitos.journal.performance_evaluator import DecisionPerformanceEvaluator
from aitos.journal.policy_monitor_service import PolicyMonitorService
from aitos.journal.repository import JournalRepository
from aitos.kernel.ai_kernel import AIKernel
from aitos.knowledge_graph.correlation_updater import SymbolCorrelationUpdater
from aitos.knowledge_graph.writer import GraphDriver, KnowledgeGraphWriter
from aitos.logging_setup import get_logger
from aitos.models.trade import TradeLifecycleState
from aitos.risk.models import PortfolioState, PositionExposure, RiskLimits
from aitos.risk.risk_engine import RiskEngine
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.reconciliation import ReconciliationScheduler
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.attention_feedback import AttentionFeedbackLoop
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.ml_feedback import MLExplainerFeedbackLoop

logger = get_logger("aitos.app")


@dataclass
class SystemComponents:
    event_bus: EventBus
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
    reconciliation: Optional[ReconciliationScheduler] = None
    knowledge_graph: Optional[KnowledgeGraphWriter] = None
    correlation_updater: Optional[SymbolCorrelationUpdater] = None
    _price_feed_subscriptions: List[Subscription] = field(default_factory=list)

    def all_modules(self) -> List[AITOSModule]:
        modules: List[AITOSModule] = [
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
        ]
        if self.knowledge_graph is not None:
            modules.append(self.knowledge_graph)
        modules += [self.data_ingestion, self.scanner, self.trade_lifecycle]
        if self.reconciliation is not None:
            modules.append(self.reconciliation)
        if self.correlation_updater is not None:
            modules.append(self.correlation_updater)
        return modules


async def build_system(
    event_bus: EventBus,
    exchange: ExchangeAdapter,
    order_executor: OrderExecutor,
    symbols: List[str],
    kline_timeframe: str = "15m",
    scanner_timeframe: str = "15m",
    market_data_repository: Optional[MarketDataRepository] = None,
    journal_repository: Optional[JournalRepository] = None,
    decision_journal_repository: Optional[DecisionJournalRepository] = None,
    graph_driver: Optional[GraphDriver] = None,
    risk_limits: Optional[RiskLimits] = None,
    kernel: Optional[AIKernel] = None,
    rl_scorer: Optional[RLPolicyScorer] = None,
    outcome_classifier: Optional[TradeOutcomeClassifier] = None,
    attention_explainer: Optional[AttentionExplainer] = None,
    use_exchange_side_stops: bool = False,
    min_score_threshold: float = 60.0,
    top_n: int = 5,
) -> SystemComponents:
    kernel = kernel or AIKernel(event_bus=event_bus)
    risk_engine = RiskEngine(event_bus=event_bus, limits=risk_limits)
    rl_scorer = rl_scorer or TabularBanditRLScorer()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=symbols,
        timeframe=scanner_timeframe,
        rl_scorer=rl_scorer,
        min_score_threshold=min_score_threshold,
        top_n=top_n,
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
    trade_lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=order_executor,
        kernel=kernel,
        use_exchange_side_stops=use_exchange_side_stops,
    )
    data_ingestion = DataIngestionService(
        exchange=exchange,
        event_bus=event_bus,
        symbols=symbols,
        kline_timeframe=kline_timeframe,
        repository=market_data_repository,
    )
    decision_journal = decision_journal_repository or DecisionJournalRepository()
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


async def initialize_all(components: SystemComponents, *, timeout: float = 5.0) -> None:
    for module in components.all_modules():
        await module.initialize({})
    components._price_feed_subscriptions = [
        await components.event_bus.subscribe(
            "market.kline.*",
            components.trade_lifecycle.handle_event,
            group="trade-lifecycle-prices",
        ),
        await components.event_bus.subscribe(
            "market.trade.*",
            components.trade_lifecycle.handle_event,
            group="trade-lifecycle-prices",
        ),
    ]
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
    _peak_equity_usd: float = field(init=False)

    def __post_init__(self) -> None:
        self._peak_equity_usd = self.starting_equity_usd

    def build_portfolio_state(self, trade_lifecycle: TradeLifecycle) -> PortfolioState:
        closed, open_trades = (
            trade_lifecycle.get_closed_trades(),
            trade_lifecycle.get_open_trades(),
        )
        realized_pnl = sum(t.pnl for t in closed if t.pnl is not None)
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
                symbol=t.symbol, notional_usd=t.position_size_usd, leverage=t.leverage
            )
            for t in open_trades
        )
        regime_counts: Dict[str, int] = {}
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
        self._peak_equity_usd: Optional[float] = None
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
        regime_counts: Dict[str, int] = {}
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
    approved_by: Optional[str] = None,
) -> int:
    refresh = getattr(portfolio_tracker, "refresh_equity", None)
    if refresh is not None:
        await refresh()
    portfolio = portfolio_tracker.build_portfolio_state(components.trade_lifecycle)
    await components.risk_engine.assess(portfolio)
    candidates = await components.scanner.scan_all()
    for candidate in candidates:
        logger.info(
            "scanner score breakdown",
            extra={
                "aitos_extra": {
                    "symbol": candidate.symbol,
                    "direction": candidate.direction.value,
                    "raw_component_scores": dict(candidate.component_scores),
                    "weights": dict(components.scanner._weights),
                    "weighted_contributions": {
                        name: round(
                            float(score) * float(components.scanner._weights.get(name, 0.0)),
                            6,
                        )
                        for name, score in candidate.component_scores.items()
                    },
                    "weight_total": sum(
                        float(components.scanner._weights.get(name, 0.0))
                        for name in candidate.component_scores
                    ),
                    "normalized_score": candidate.composite_score,
                    "threshold": components.scanner._min_score_threshold,
                    "threshold_pass": candidate.composite_score
                    >= components.scanner._min_score_threshold,
                }
            },
        )
    ranked = await components.scanner.rank(candidates)
    logger.info(
        "scanner ranking decision",
        extra={
            "aitos_extra": {
                "candidate_count": len(candidates),
                "ranked_count": len(ranked),
                "threshold": components.scanner._min_score_threshold,
                "ranked_symbols": [c.symbol for c in ranked],
                "candidate_scores": {
                    c.symbol: c.composite_score for c in candidates
                },
            }
        },
    )
    open_symbols = {t.symbol for t in components.trade_lifecycle.get_open_trades()}
    submitted = 0
    for candidate in ranked:
        if candidate.symbol in open_symbols:
            logger.info(
                "trade candidate skipped",
                extra={
                    "aitos_extra": {
                        "symbol": candidate.symbol,
                        "reason": "symbol_already_open",
                        "score": candidate.composite_score,
                    }
                },
            )
            continue
        decision = await components.scanner.decide_with_kernel(
            candidate, components.kernel
        )
        if (
            decision.direction != candidate.direction.value.lower()
            or decision.confidence < components.kernel.fusion_min_confidence
        ):
            logger.info(
                "trade candidate rejected by kernel",
                extra={
                    "aitos_extra": {
                        "symbol": candidate.symbol,
                        "score": candidate.composite_score,
                        "candidate_direction": candidate.direction.value.lower(),
                        "kernel_direction": decision.direction,
                        "kernel_confidence": decision.confidence,
                        "kernel_min_confidence": components.kernel.fusion_min_confidence,
                        "direction_match": decision.direction
                        == candidate.direction.value.lower(),
                        "confidence_pass": decision.confidence
                        >= components.kernel.fusion_min_confidence,
                    }
                },
            )
            continue
        opportunity = components.scanner.to_opportunity(
            candidate, is_production=is_production, approved_by=approved_by
        )
        decision_id = opportunity.opportunity_id
        opportunity = replace(
            opportunity,
            confidence=min(opportunity.confidence, decision.confidence),
            rationale=f"kernel_confidence={decision.confidence:.4f}; "
            + opportunity.rationale,
            agent_consensus={
                **opportunity.agent_consensus,
                "kernel_fusion_confidence": decision.confidence,
                "decision_id": decision_id,
            },
        )
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
                },
                source_module="aitos.app",
            )
        )
        trade = await components.trade_lifecycle.submit_opportunity(
            opportunity,
            portfolio_tracker.build_portfolio_state(components.trade_lifecycle),
        )
        submitted += 1
        logger.info(
            "trade candidate submitted",
            extra={
                "aitos_extra": {
                    "symbol": candidate.symbol,
                    "score": candidate.composite_score,
                    "kernel_confidence": decision.confidence,
                    "trade_state": trade.state.value,
                }
            },
        )
        if trade.state == TradeLifecycleState.POSITION_OPENED:
            open_symbols.add(candidate.symbol)
    return submitted
