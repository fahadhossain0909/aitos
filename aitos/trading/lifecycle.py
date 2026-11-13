"""Trade Lifecycle — spec section 30.

    [OPPORTUNITY_DETECTED]
        v (Smart Entry Validation passes)
    [ENTRY_VALIDATED]
        v (Dynamic Position Sizing + Adaptive Leverage calculated)
    [ORDER_SUBMITTED]
        v (Order filled)
    [POSITION_OPENED]
        v  (Smart SL/TP, trailing SL, break-even, partial TP monitored via update_price)
        v  (optional Exit Intelligence / Position Manager — HOLD / MANAGE / EXIT)
    [EXIT_TRIGGERED]
        v (Order filled)
    [POSITION_CLOSED]

Journal/Review/Learning-feedback stages are a later phase (Journal System).
This module owns validation (Risk Engine veto + hard limits + governance),
sizing, order submission, and exit monitoring — wiring the Risk Engine and
AI Kernel into an actual trade, end to end.

Phase E (Market-Path architecture): an optional PositionManager may be
injected. With EIE enabled, the initial strategy SL is a soft decision
boundary; a separately sized emergency stop remains authoritative. A soft
stop breach forces EIE evaluation, and HOLD may retain the position until
thesis invalidation or the emergency boundary. Without EIE, legacy SL behavior
is unchanged. Static TP full-exit can be deferred on HOLD; MANAGE may
partial-reduce or tighten protection.
"""

# NOTE: this file previously shipped as a zlib+base64-packed blob that was
# exec()'d at import time ("packed to fit transport limits"). That made the
# trade-lifecycle state machine -- including the hard-SL check below --
# invisible to grep, IDE "find usages", static analysis, and code review
# diffs. Restored to plain source here with zero logic changes (mechanically
# decompressed and verified byte-for-byte identical to the packed version's
# expansion, then the full test suite was re-run against it).

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aitos.config.paths import get_model_data_dir
from aitos.config.tuning import TuningSettings, load_tuning_settings
from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError, TradeNotFoundError
from aitos.eventbus.redis_bus import EventBus
from aitos.execution.intent_store import (
    ExecutionRecoveryRequired,
    ExecutionUncertainError,
)
from aitos.execution.order_executor import (
    OrderExecutor,
    OrderRequest,
    OrderResult,
    SimulatedOrderExecutor,
)
from aitos.kernel.ai_kernel import Action, AIKernel
from aitos.logging_setup import get_logger
from aitos.models.trade import (
    Opportunity,
    PartialExit,
    Trade,
    TradeLifecycleState,
    TradeSide,
    new_trade_id,
    utc_now_iso,
)
from aitos.risk.models import PortfolioState, PositionExposure, PositionSizeResult
from aitos.risk.risk_engine import RiskEngine
from aitos.statistics.contracts import FeatureSnapshot, ForecastEnvelope
from aitos.statistics.decision_ledger import DecisionOutcomeLedger
from aitos.statistics.financial_ml_pipeline import FinancialMLPipeline
from aitos.statistics.risk_overlay import assess_statistical_risk_forecast
from aitos.statistics.shadow import StatisticalShadowLedger
from aitos.trading.tp_contract import TakeProfitExecutionContract

if TYPE_CHECKING:
    from aitos.intelligence.amt.volume_profile import VolumeProfile
    from aitos.intelligence.liquidity_tracker import LiquidityEvent
    from aitos.intelligence.order_flow_engine import OrderFlowFeatures
    from aitos.trading.market_context import MarketContextProvider
    from aitos.trading.position_manager import PositionManager

logger = get_logger("aitos.trading.lifecycle")

TOPIC_OPPORTUNITY = "decision.opportunity"
TOPIC_ENTRY_SIGNAL = "decision.entry"
TOPIC_REJECTED = "trade.rejected"
TOPIC_ORDER_SUBMITTED = "trade.order_submitted"
TOPIC_ORDER_FILLED = "trade.order_filled"
TOPIC_EXECUTION_RECONCILIATION_REQUIRED = "trade.execution_reconciliation_required"
TOPIC_POSITION_OPENED = "trade.position_opened"
TOPIC_POSITION_UPDATED = "trade.position_updated"
TOPIC_COST_ADJUSTMENT = "trade.cost_adjustment"
TOPIC_POSITION_CLOSED = "trade.position_closed"
TOPIC_SL_TRIGGERED = "trade.sl_triggered"
TOPIC_TP_TRIGGERED = "trade.tp_triggered"
TOPIC_TRAILING_SL_UPDATE = "trade.trailing_sl"
TOPIC_PARTIAL_CLOSE = "trade.partial_close"
TOPIC_EXCHANGE_STOPS_PLACED = "trade.exchange_stops_placed"
TOPIC_EXIT_DECISION = "decision.exit"
DEFAULT_PARTIAL_CLOSE_FRACTION = 0.5


def _valid_market_price(value: Any) -> bool:
    """Return True only for finite, strictly positive market prices."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0.0


class TradeLifecycle(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        risk_engine: RiskEngine,
        order_executor: OrderExecutor | None = None,
        kernel: AIKernel | None = None,
        use_exchange_side_stops: bool = False,
        position_manager: PositionManager | None = None,
        market_context_provider: MarketContextProvider | None = None,
        statistical_shadow_ledger: StatisticalShadowLedger | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._risk_engine = risk_engine
        self._order_executor = order_executor or SimulatedOrderExecutor()
        self._kernel = kernel
        self._use_exchange_side_stops = (
            use_exchange_side_stops
            and self._order_executor.supports_exchange_side_stops
        )
        # Optional Exit-Intelligence stack (Phase E). When None, behaviour is
        # identical to the pre-Phase-E lifecycle.
        self._position_manager = position_manager
        self._market_context_provider = market_context_provider
        self._statistical_shadow_ledger = statistical_shadow_ledger
        self._tuning = load_tuning_settings()
        # Fair, per-position cadence: one busy symbol must not consume another
        # position's exit-analysis allowance.
        self._last_intensive_analysis_at: dict[str, float] = {}
        self._last_partial_exit_at: dict[str, float] = {}
        self._soft_stop_breached: set[str] = set()
        self._tp_boundary_breached: set[str] = set()
        self._initialized = False
        self._open_trades: dict[str, Trade] = {}
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._price_update_locks: dict[str, asyncio.Lock] = {}
        self._execution_faults: dict[str, str] = {}
        self._confirmed_reductions: dict[str, tuple[str, OrderResult]] = {}
        self._accepting_live_submissions = True
        self._closed_trades: list[Trade] = []
        self._position_switch_coordinator: Any | None = None
        self._last_event_time: str | None = None

    def _shadow_ledger(self) -> StatisticalShadowLedger:
        if self._statistical_shadow_ledger is None:
            self._statistical_shadow_ledger = StatisticalShadowLedger(
                get_model_data_dir() / "statistical_shadow.sqlite3"
            )
        return self._statistical_shadow_ledger

    def _decision_ledger(self) -> DecisionOutcomeLedger:
        if not hasattr(self, "_decision_outcome_ledger"):
            self._decision_outcome_ledger = DecisionOutcomeLedger(
                get_model_data_dir() / "decision_outcomes.sqlite3"
            )
        return self._decision_outcome_ledger

    def _financial_ml_pipeline(self) -> FinancialMLPipeline:
        if not hasattr(self, "_financial_ml"):
            self._financial_ml = FinancialMLPipeline(
                get_model_data_dir() / "financial_ml.sqlite3"
            )
        return self._financial_ml

    @staticmethod
    def _trade_attribution_id(trade: Trade) -> str:
        candidate_id = trade.agent_consensus.get("candidate_id")
        if candidate_id:
            return str(candidate_id)
        trace_id = trade.agent_consensus.get("trace_id")
        return (
            f"{str(trace_id).strip()}:{trade.side.value}"
            if trace_id
            else trade.trade_id
        )

    async def _record_decision(
        self,
        decision_id: str,
        *,
        entity_id: str,
        symbol: str,
        side: str,
        decision_type: str,
        accepted: bool,
        reason: str,
        payload: Mapping[str, Any] | None = None,
        decision_at: str | None = None,
    ) -> None:
        try:
            candidate_id = (
                str(payload.get("candidate_id"))
                if isinstance(payload, Mapping) and payload.get("candidate_id")
                else (
                    entity_id
                    if len(entity_id) == 32
                    and all(
                        character in "0123456789abcdef"
                        for character in entity_id.lower()
                    )
                    else None
                )
            )
            record_payload = dict(payload or {})
            if candidate_id:
                record_payload.setdefault("candidate_id", candidate_id)
            await asyncio.to_thread(
                self._decision_ledger().record_decision,
                decision_id,
                entity_id=entity_id,
                symbol=symbol,
                side=side,
                decision_type=decision_type,
                accepted=accepted,
                reason=reason,
                payload=record_payload,
                decision_at=decision_at,
                candidate_id=candidate_id,
            )
        except Exception:
            logger.exception(
                "decision attribution persistence failed",
                extra={"aitos_extra": {"decision_id": decision_id, "symbol": symbol}},
            )

    async def _resolve_decision_outcomes(self, trade: Trade) -> None:
        if trade.pnl_percent is None:
            return
        try:
            await asyncio.to_thread(
                self._decision_ledger().resolve_entity,
                self._trade_attribution_id(trade),
                outcome_value=float(trade.pnl_percent) / 100.0,
                outcome_at=str(trade.exit_time or utc_now_iso()),
                outcome={
                    "provenance": "EXECUTED_TRADE_NET_OUTCOME",
                    "counterfactual": False,
                    "net_pnl_usd": float(trade.pnl),
                    "pnl_percent": float(trade.pnl_percent),
                    "close_reason": str(getattr(trade, "close_reason", "") or ""),
                    "fees_paid_usd": float(trade.fees_paid_usd),
                    "funding_pnl_usd": float(trade.funding_pnl_usd),
                },
                candidate_id=(
                    str(trade.agent_consensus.get("candidate_id"))
                    if trade.agent_consensus.get("candidate_id")
                    else None
                ),
                outcome_provenance="EXECUTED_TRADE_NET_OUTCOME",
            )
            candidate_id = trade.agent_consensus.get("candidate_id")
            if candidate_id:
                resolved = await asyncio.to_thread(
                    self._financial_ml_pipeline().resolve_executed,
                    str(candidate_id),
                    net_return=float(trade.pnl_percent) / 100.0,
                    resolved_at=str(trade.exit_time or utc_now_iso()),
                    trade_id=trade.trade_id,
                    outcome={
                        "net_pnl_usd": float(trade.pnl),
                        "fees_paid_usd": float(trade.fees_paid_usd),
                        "funding_pnl_usd": float(trade.funding_pnl_usd),
                        "close_reason": str(getattr(trade, "close_reason", "") or ""),
                    },
                )
                if not resolved:
                    logger.error(
                        "closed trade did not resolve its Financial ML candidate",
                        extra={
                            "aitos_extra": {
                                "candidate_id": candidate_id,
                                "trade_id": trade.trade_id,
                            }
                        },
                    )
        except Exception:
            logger.exception(
                "decision outcome reconciliation failed",
                extra={"aitos_extra": {"trade_id": trade.trade_id}},
            )

    def _record_statistical_forecast(self, trade: Trade) -> bool:
        forecast = trade.agent_consensus.get("statistical_forecast")
        if not isinstance(forecast, Mapping):
            return False
        as_of = str(forecast["as_of"])
        sample_count = int(forecast["sample_size"])
        feature_values = {
            str(key): float(value)
            for key, value in trade.agent_consensus.items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        }
        feature_values.update(
            {
                f"statistical_{key}": float(value)
                for key, value in forecast.items()
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            }
        )
        feature_snapshot_id = str(
            forecast.get("feature_snapshot_id")
            or f"{trade.symbol}:{forecast['horizon']}:{as_of}"
        )
        snapshot = FeatureSnapshot(
            symbol=trade.symbol,
            horizon=str(forecast["horizon"]),
            as_of=as_of,
            window_start=str(forecast.get("window_start") or as_of),
            values=feature_values,
            source_watermarks={"scanner": as_of},
            sample_count=sample_count,
        )
        envelope = ForecastEnvelope(
            symbol=trade.symbol,
            horizon=str(forecast["horizon"]),
            side=str(forecast["side"]),
            model_id=str(forecast["model_id"]),
            model_version=str(forecast["model_version"]),
            as_of=as_of,
            feature_snapshot_id=feature_snapshot_id,
            sample_count=sample_count,
            expected_return=float(forecast["expected_return"]),
            expected_volatility=float(forecast["forecast_volatility"]),
            downside_probability=float(forecast["downside_probability"]),
            tail_probability=float(forecast["tail_probability"]),
            expected_shortfall=(
                None
                if forecast.get("expected_shortfall") is None
                else float(forecast["expected_shortfall"])
            ),
        )
        return self._shadow_ledger().record_forecast(
            trade.trade_id,
            envelope,
            predicted_up=float(forecast["predicted_up"]),
            predicted_variance=max(envelope.expected_volatility**2, 1e-12),
            created_at=trade.entry_time,
            feature_snapshot=snapshot,
        )

    def _record_statistical_outcome(self, trade: Trade) -> bool:
        if not isinstance(trade.agent_consensus.get("statistical_forecast"), Mapping):
            return False
        if trade.pnl_percent is None:
            return False
        actual_return = float(trade.pnl_percent) / 100.0
        # A single trade's squared net return is a low-frequency variance
        # proxy, not high-frequency realised variance. Keep that limitation
        # explicit in the ledger documentation and validation interpretation.
        return self._shadow_ledger().record_outcome(
            trade.trade_id,
            actual_return=actual_return,
            realised_variance=actual_return * actual_return,
            resolved_at=trade.exit_time,
        )

    def apply_tuning_settings(self, settings: TuningSettings) -> None:
        """Apply an explicitly reloaded tuning snapshot to position monitoring."""
        self._tuning = settings
        self._last_intensive_analysis_at.clear()
        self._last_partial_exit_at.clear()
        self._soft_stop_breached.clear()
        self._tp_boundary_breached.clear()

    def _allow_intensive_analysis(self, trade_id: str, *, force: bool = False) -> bool:
        """Throttle per position, never globally across unrelated open trades.

        A soft-stop or TP boundary forces an immediate evaluation. Ordinary
        observations are evaluated at the configured per-position cadence.
        """
        now = time.monotonic()
        interval = self._tuning.positions.monitoring_interval_seconds
        last = self._last_intensive_analysis_at.get(trade_id)
        if force or last is None or now - last >= interval:
            self._last_intensive_analysis_at[trade_id] = now
            return True
        return False

    def _partial_exit_allowed(
        self, trade: Trade, close_fraction: float
    ) -> tuple[bool, str]:
        """Guard repeated reductions and prevent economically meaningless dust."""
        positions = self._tuning.positions
        max_count = int(getattr(positions, "max_partial_exit_count", 3))
        cooldown = float(getattr(positions, "partial_exit_cooldown_seconds", 60.0))
        minimum_remaining = float(getattr(positions, "min_remaining_position_usd", 1.0))
        if max_count <= 0 or len(trade.partial_exits) >= max_count:
            return False, "partial_exit_count_limit"
        now = time.monotonic()
        last = self._last_partial_exit_at.get(trade.trade_id)
        # PartialExit.at is persisted with the trade, so cooldown survives a
        # process restart even when the in-memory monotonic clock is reset.
        if trade.partial_exits:
            try:
                persisted_at = datetime.fromisoformat(
                    trade.partial_exits[-1].at.replace("Z", "+00:00")
                )
                if persisted_at.tzinfo is None:
                    persisted_at = persisted_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - persisted_at).total_seconds()
                if elapsed < cooldown:
                    return False, "partial_exit_cooldown"
            except (TypeError, ValueError):
                # Fall back to this-process monotonic timestamp for malformed
                # legacy records; the count limit still protects the trade.
                pass
        if last is not None and now - last < cooldown:
            return False, "partial_exit_cooldown"
        remaining = trade.position_size_usd * (1.0 - close_fraction)
        if remaining < minimum_remaining:
            return False, "partial_exit_would_create_dust"
        return True, "confirmed_partial_exit"

    @property
    def module_id(self) -> str:
        return "trade-lifecycle"

    @property
    def version(self) -> str:
        return "1.2.0"

    @property
    def position_manager(self) -> PositionManager | None:
        return self._position_manager

    @property
    def market_context_provider(self) -> MarketContextProvider | None:
        return self._market_context_provider

    @market_context_provider.setter
    def market_context_provider(self, provider: MarketContextProvider | None) -> None:
        self._market_context_provider = provider

    async def recover_position_switches(
        self,
        *,
        trades_hydrated: bool,
        venue_positions_reconciled: bool,
    ) -> Any:
        """Recover switch intents only after both local and venue state are ready."""
        if not trades_hydrated or not venue_positions_reconciled:
            raise RuntimeError(
                "position-switch recovery requires hydrated trades and reconciled venue positions"
            )
        coordinator = self._position_switch_coordinator
        if coordinator is None:
            raise RuntimeError(
                "position-switch recovery requested without a configured coordinator"
            )
        from aitos.trading.switch_recovery import PositionSwitchRecoveryWorker

        self._switch_recovery_report = await PositionSwitchRecoveryWorker(
            coordinator
        ).recover_pending()
        logger.info(
            "Position-switch startup recovery completed",
            extra={
                "aitos_extra": {
                    "scanned": self._switch_recovery_report.scanned,
                    "resumed": self._switch_recovery_report.resumed,
                    "pending": self._switch_recovery_report.pending,
                    "manual_reconciliation": self._switch_recovery_report.manual_reconciliation,
                }
            },
        )
        return self._switch_recovery_report

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        # Recovery uses normal lifecycle close/open methods, which require the
        # module to be initialized. Roll back the flag if recovery fails so the
        # caller can correct the startup dependency and retry initialization.
        self._initialized = True
        try:
            if config.get("recover_position_switches", False):
                await self.recover_position_switches(
                    trades_hydrated=bool(config.get("trades_hydrated", False)),
                    venue_positions_reconciled=bool(
                        config.get("venue_positions_reconciled", False)
                    ),
                )
        except Exception:
            self._initialized = False
            raise
        logger.info(
            "TradeLifecycle initialized",
            extra={
                "aitos_extra": {
                    "exit_intelligence": self._position_manager is not None,
                    "position_switch_recovery": (
                        getattr(self, "_switch_recovery_report", None) is not None
                    ),
                }
            },
        )

    async def health_check(self) -> HealthStatus:
        unresolved = False
        storage_error = False
        try:
            unresolved = self._order_executor.has_unresolved_orders()
        except Exception:
            storage_error = True
        account_mismatch = bool(getattr(self, "_account_reconciliation_error", False))
        execution_fault = (
            bool(self._execution_faults)
            or storage_error
            or account_mismatch
            or bool(getattr(self, "_execution_recovery_required", False))
        )
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.UNHEALTHY
                if not self._initialized or execution_fault
                else ModuleStatus.DEGRADED if unresolved else ModuleStatus.HEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "open_trades": len(self._open_trades),
                "closed_trades": len(self._closed_trades),
                "unresolved_execution": unresolved,
                "execution_faults": len(self._execution_faults),
                "execution_storage_error": storage_error,
                "account_reconciliation_error": account_mismatch,
                "live_submissions": len(getattr(self, "_live_submission_tasks", set())),
                "exit_intelligence": self._position_manager is not None,
                "market_context": self._market_context_provider is not None,
            },
        )

    async def drain_live_submissions(self, grace_period_seconds: float = 30.0) -> None:
        self._accepting_live_submissions = False
        tasks = set(getattr(self, "_live_submission_tasks", set()))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_period_seconds)
            if pending:
                raise ExecutionUncertainError(
                    "runtime", "shutdown", "live submissions did not drain"
                )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        await self.drain_live_submissions(grace_period_seconds)
        funnel_store = getattr(self, "_aitos_directional_funnel", None)
        if funnel_store is not None:
            funnel_store.close()
            delattr(self, "_aitos_directional_funnel")
        financial_ml = getattr(self, "_financial_ml", None)
        if financial_ml is not None:
            financial_ml.close()
            delattr(self, "_financial_ml")
        gateway = getattr(self, "_aitos_capital_gateway", None)
        if gateway is not None:
            close_calibrator = getattr(gateway.calibrator, "close", None)
            if callable(close_calibrator):
                close_calibrator()
            close_reservation = getattr(gateway.reservation, "close", None)
            if callable(close_reservation):
                close_reservation()
        logger.info(
            "TradeLifecycle shut down",
            extra={"aitos_extra": {"open_trades": len(self._open_trades)}},
        )

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield

    async def handle_event(self, event: Event) -> EventResponse | None:
        if not self._initialized:
            return None
        if event.topic == TOPIC_COST_ADJUSTMENT:
            payload = event.payload
            await self.apply_cost_adjustment(
                str(payload.get("trade_id", "")),
                adjustment_id=str(payload.get("adjustment_id", "")),
                kind=str(payload.get("kind", "")),
                amount_usd=float(payload.get("amount_usd", 0.0)),
            )
            return None
        if event.topic.startswith("market.kline.") or event.topic.startswith(
            "market.trade."
        ):
            symbol = event.payload.get("symbol")
            price = event.payload.get("close", event.payload.get("price"))
            if symbol and _valid_market_price(price):
                current_price = float(price)
                ctx_kwargs: dict[str, Any] = {}
                if self._market_context_provider is not None:
                    try:
                        ctx = self._market_context_provider.get_context(symbol)
                        ctx_kwargs = ctx.as_kwargs()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "market context unavailable for %s: %s", symbol, exc
                        )
                for trade in list(self._open_trades.values()):
                    if trade.symbol == symbol:
                        await self.update_price(
                            trade.trade_id, current_price, **ctx_kwargs
                        )
            elif symbol and price is not None:
                logger.warning(
                    "Ignoring invalid market price",
                    extra={"aitos_extra": {"symbol": symbol, "price": price}},
                )
        return None

    def get_open_trades(self) -> list[Trade]:
        return list(self._open_trades.values())

    def get_closed_trades(self) -> list[Trade]:
        return list(self._closed_trades)

    async def submit_opportunity(
        self, opportunity: Opportunity, portfolio: PortfolioState
    ) -> Trade:
        """Measure opportunity-to-lifecycle handling; preserve existing policy."""
        import time

        started = time.perf_counter()
        symbol = str(getattr(opportunity, "symbol", "") or "")
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_error,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            trace_id = str(
                opportunity.agent_consensus.get("trace_id")
                or f"op:{symbol.upper()}:{opportunity.opportunity_id}"
            )
            parent_trace_id = opportunity.agent_consensus.get("parent_trace_id")
            record_input(PipelineStage.TRADE_LIFECYCLE.value, symbol=symbol)
            note_trace(
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                stage=PipelineStage.TRADE_LIFECYCLE.value,
                symbol=symbol,
                event_id=opportunity.opportunity_id,
                extra={"phase": "opportunity_submitted"},
            )
        except Exception:
            pass
        try:
            trade = await self._submit_opportunity_impl(opportunity, portfolio)
        except Exception as exc:
            trace_id = opportunity.agent_consensus.get("trace_id")
            await self._record_decision(
                f"{opportunity.opportunity_id}:EXECUTION_FAILURE:{time.time_ns()}",
                entity_id=(
                    f"{str(trace_id).strip()}:{opportunity.side.value}"
                    if trace_id
                    else opportunity.opportunity_id
                ),
                symbol=symbol or "UNKNOWN",
                side=opportunity.side.value,
                decision_type="EXECUTION_FAILURE",
                accepted=False,
                reason=f"opportunity_submission_failed:{type(exc).__name__}",
                payload={
                    "opportunity_id": opportunity.opportunity_id,
                    "error_type": type(exc).__name__,
                },
            )
            try:
                record_error(
                    PipelineStage.TRADE_LIFECYCLE.value,
                    symbol=symbol,
                    issue="processing_error",
                )
            except Exception:
                pass
            raise
        try:
            latency_ms = (time.perf_counter() - started) * 1000.0
            record_output(
                PipelineStage.TRADE_LIFECYCLE.value,
                symbol=symbol,
                latency_ms=latency_ms,
            )
            note_trace(
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                stage=PipelineStage.TRADE_LIFECYCLE.value,
                symbol=symbol,
                latency_ms=latency_ms,
                event_id=opportunity.opportunity_id,
                extra={
                    "phase": "opportunity_completed",
                    "trade_id": trade.trade_id,
                    "state": trade.state.value,
                },
            )
        except Exception:
            pass
        return trade

    async def _submit_opportunity_impl(
        self, opportunity: Opportunity, portfolio: PortfolioState
    ) -> Trade:
        self._require_initialized()
        await self._event_bus.publish(
            Event(
                topic=TOPIC_OPPORTUNITY,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "confidence": opportunity.confidence,
                },
                source_module=self.module_id,
            )
        )
        should_veto, veto_reason = self._risk_engine.veto(portfolio)
        if should_veto:
            return await self._reject(opportunity, f"risk veto: {veto_reason}")
        hard_breaches = [
            b for b in self._risk_engine.check_limits(portfolio) if b.is_hard_cap
        ]
        if hard_breaches:
            return await self._reject(
                opportunity, f"hard limit breach: {hard_breaches[0].message}"
            )
        if opportunity.is_production and self._kernel is not None:
            action = Action(
                action_type="order.submit",
                payload={"symbol": opportunity.symbol, "side": opportunity.side.value},
                is_production=True,
                approved_by=opportunity.approved_by,
            )
            governance = await self._kernel.enforce_governance(action)
            if not governance.approved:
                return await self._reject(
                    opportunity, f"governance denied: {governance.reason}"
                )
        return await self._validate_and_open(opportunity, portfolio)

    def _emergency_stop_price(self, trade: Trade) -> float:
        """Return the independent catastrophic stop used while EIE is active."""
        configured = getattr(trade, "emergency_sl_price", None)
        if configured is not None and _valid_market_price(configured):
            return float(configured)
        multiple = float(
            getattr(self._tuning.positions, "emergency_stop_r_multiple", 2.0)
        )
        distance = trade.r_distance * multiple
        if distance <= 0:
            return trade.sl_price
        return (
            trade.entry_price - distance
            if trade.side == TradeSide.LONG
            else trade.entry_price + distance
        )

    async def update_price(
        self, trade_id: str, current_price: float, **kwargs: Any
    ) -> Trade | None:
        if not self._order_executor.requires_exchange_execution:
            return await self._update_price_impl(trade_id, current_price, **kwargs)
        async with self._price_update_locks.setdefault(trade_id, asyncio.Lock()):
            return await self._update_price_impl(trade_id, current_price, **kwargs)

    async def _update_price_impl(
        self,
        trade_id: str,
        current_price: float,
        *,
        order_flow: OrderFlowFeatures | None = None,
        volume_profile: VolumeProfile | None = None,
        liquidity_events: Sequence[LiquidityEvent] = (),
        prior_highs: Sequence[float] = (),
        prior_lows: Sequence[float] = (),
        swing_highs: Sequence[float] = (),
        swing_lows: Sequence[float] = (),
        structure_break_level: float | None = None,
        atr: float | None = None,
        trend_strength: float | None = None,
        extra_features: Mapping[str, float] | None = None,
    ) -> Trade:
        self._require_initialized()
        if not _valid_market_price(current_price):
            raise ValueError(
                f"current_price must be a finite positive number, got {current_price!r}"
            )
        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        if trade.state != TradeLifecycleState.POSITION_OPENED:
            return trade
        capital_gateway = getattr(self, "_aitos_capital_gateway", None)
        if capital_gateway is not None and not capital_gateway.renew(trade.symbol):
            logger.error(
                "open-position capital reservation lease could not be renewed",
                extra={"aitos_extra": {"trade_id": trade.trade_id}},
            )

        # Record every valid market observation before evaluating exits so
        # MAE/MFE includes the terminal price that triggers SL/TP as well.
        trade.record_excursion(float(current_price))
        trade.last_marked_price = current_price
        trade.last_marked_at = datetime.now(timezone.utc).isoformat()
        is_long = trade.side == TradeSide.LONG

        # The scanner's initial SL is a soft risk boundary while EIE is active.
        # It triggers an urgent thesis evaluation; it is not allowed to bypass
        # HOLD/MANAGE. A separate, wider emergency-loss boundary remains
        # authoritative. Without EIE, preserve the legacy protective SL.
        sl_hit = (
            current_price <= trade.sl_price
            if is_long
            else current_price >= trade.sl_price
        )
        emergency_r = float(
            getattr(self._tuning.positions, "emergency_stop_r_multiple", 2.0)
        )
        emergency_price = self._emergency_stop_price(trade)
        emergency_stop_hit = self._position_manager is not None and (
            current_price <= emergency_price
            if is_long
            else current_price >= emergency_price
        )
        adverse_r = max(0.0, -trade.unrealized_r_multiple(current_price))
        if emergency_stop_hit:
            logger.warning(
                "position emergency loss boundary reached",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "adverse_r": adverse_r,
                        "emergency_stop_r_multiple": emergency_r,
                    }
                },
            )
            return await self._trigger_exit(
                trade, current_price, "emergency_risk_limit", TOPIC_SL_TRIGGERED
            )
        if sl_hit and self._position_manager is None:
            return await self._trigger_exit(
                trade, current_price, "sl_triggered", TOPIC_SL_TRIGGERED
            )

        # A breached soft stop and a reached TP are urgent decision points.
        # Otherwise each position receives its own regular EIE evaluation slot.
        tp_contract = TakeProfitExecutionContract(
            intelligent=self._position_manager is not None
        )
        tp_boundary_hit = bool(
            trade.take_profit_levels
            and tp_contract.reached(
                trade.side, current_price, float(trade.take_profit_levels[0])
            )
        )

        # Force one immediate evaluation on each boundary crossing, then return
        # to the per-position cadence while price remains beyond that boundary.
        soft_stop_newly_breached = (
            sl_hit and trade.trade_id not in self._soft_stop_breached
        )
        if sl_hit:
            self._soft_stop_breached.add(trade.trade_id)
        else:
            self._soft_stop_breached.discard(trade.trade_id)
        tp_newly_breached = (
            tp_boundary_hit and trade.trade_id not in self._tp_boundary_breached
        )
        if tp_boundary_hit:
            self._tp_boundary_breached.add(trade.trade_id)
        else:
            self._tp_boundary_breached.discard(trade.trade_id)

        eie_action = None
        if self._position_manager is not None and self._allow_intensive_analysis(
            trade.trade_id,
            force=soft_stop_newly_breached or tp_newly_breached,
        ):
            try:
                eie_action = await self._apply_exit_intelligence(
                    trade,
                    current_price,
                    order_flow=order_flow,
                    volume_profile=volume_profile,
                    liquidity_events=liquidity_events,
                    prior_highs=prior_highs,
                    prior_lows=prior_lows,
                    swing_highs=swing_highs,
                    swing_lows=swing_lows,
                    structure_break_level=structure_break_level,
                    atr=atr,
                    trend_strength=trend_strength,
                    extra_features=extra_features,
                )
            except Exception:
                logger.exception(
                    "exit intelligence evaluation failed",
                    extra={
                        "aitos_extra": {
                            "trade_id": trade.trade_id,
                            "symbol": trade.symbol,
                            "soft_stop_breached": sl_hit,
                        }
                    },
                )
                # If the soft stop is already breached and the decision system
                # is unavailable, fail closed at the protective boundary.
                if sl_hit:
                    return await self._trigger_exit(
                        trade,
                        current_price,
                        "sl_triggered_eie_unavailable",
                        TOPIC_SL_TRIGGERED,
                    )
                raise
            if eie_action is not None:
                return eie_action

        if sl_hit:
            # EIE has explicitly chosen HOLD or MANAGE. Continue monitoring,
            # but never loosen the existing protective stop here.
            logger.info(
                "soft stop breached; position retained by exit intelligence",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "price": current_price,
                        "stop_price": trade.sl_price,
                        "last_eie_action": getattr(trade, "_last_eie_action", None),
                        "adverse_r": adverse_r,
                    }
                },
            )

        # ---- TP handling ------------------------------------------------------
        # With EIE enabled, target prices are observation/checkpoint levels only.
        # They never cause an automatic full exit or static partial reduction.
        # EIE may explicitly EXIT or MANAGE (including a partial reduction) after
        # evaluating the live thesis, path, order flow and structure.
        if tp_contract.tp_is_checkpoint and tp_boundary_hit:
            reached_tp = trade.take_profit_levels.pop(0)
            self._tp_boundary_breached.discard(trade.trade_id)
            trade.updated_at = utc_now_iso()
            await self._event_bus.publish(
                Event(
                    topic=TOPIC_POSITION_UPDATED,
                    payload={
                        **trade.to_dict(),
                        "reason": "eie_tp_checkpoint_consumed",
                        "reached_tp": reached_tp,
                    },
                    source_module=self.module_id,
                )
            )
            logger.info(
                "EIE TP checkpoint reached; target consumed without automatic exit",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "tp_checkpoint": reached_tp,
                        "price": current_price,
                        "eie_action": getattr(trade, "_last_eie_action", None),
                    }
                },
            )
        elif tp_contract.static_tp_may_close and trade.take_profit_levels:
            # Preserve legacy static TP behavior when Exit Intelligence is off.
            next_tp = trade.take_profit_levels[0]
            tp_hit = tp_contract.reached(trade.side, current_price, float(next_tp))
            if tp_hit:
                if len(trade.take_profit_levels) > 1:
                    await self._partial_close(
                        trade,
                        current_price,
                        DEFAULT_PARTIAL_CLOSE_FRACTION,
                        consume_tp=self._order_executor.requires_exchange_execution,
                    )
                    if not self._order_executor.requires_exchange_execution:
                        trade.take_profit_levels.pop(0)
                    self._tp_boundary_breached.discard(trade.trade_id)
                    if (
                        self._use_exchange_side_stops
                        and trade.tp_order_ids
                        and not self._order_executor.requires_exchange_execution
                    ):
                        consumed_order_id = trade.tp_order_ids.pop(0)
                        await self._order_executor.cancel_resting_order(
                            trade.symbol, consumed_order_id
                        )
                    return trade
                return await self._trigger_exit(
                    trade, current_price, "tp_triggered", TOPIC_TP_TRIGGERED
                )

        # ---- 4. Breakeven (unchanged) ----------------------------------------
        if (
            trade.breakeven_at_r_multiple is not None
            and not trade.breakeven_triggered
            and trade.unrealized_r_multiple(current_price)
            >= trade.breakeven_at_r_multiple
        ):
            trade.sl_price = trade.entry_price
            if self._position_manager is not None:
                current_emergency = self._emergency_stop_price(trade)
                if (
                    trade.side == TradeSide.LONG
                    and trade.entry_price > current_emergency
                ) or (
                    trade.side == TradeSide.SHORT
                    and trade.entry_price < current_emergency
                ):
                    trade.emergency_sl_price = trade.entry_price
            trade.breakeven_triggered = True
            trade.updated_at = utc_now_iso()
            if self._use_exchange_side_stops:
                await self._replace_exchange_side_stop_loss(trade)
            await self._event_bus.publish(
                Event(
                    topic=TOPIC_POSITION_UPDATED,
                    payload={**trade.to_dict(), "reason": "breakeven"},
                    source_module=self.module_id,
                )
            )

        # ---- 5. Trailing SL (unchanged) --------------------------------------
        if trade.trailing_sl_enabled:
            candidate_sl = (
                current_price - trade.r_distance
                if is_long
                else current_price + trade.r_distance
            )
            improved = (is_long and candidate_sl > trade.sl_price) or (
                not is_long and candidate_sl < trade.sl_price
            )
            if improved:
                trade.sl_price = candidate_sl
                if self._position_manager is not None:
                    current_emergency = self._emergency_stop_price(trade)
                    if (
                        trade.side == TradeSide.LONG
                        and candidate_sl > current_emergency
                    ) or (
                        trade.side == TradeSide.SHORT
                        and candidate_sl < current_emergency
                    ):
                        trade.emergency_sl_price = candidate_sl
                trade.updated_at = utc_now_iso()
                if self._use_exchange_side_stops:
                    await self._replace_exchange_side_stop_loss(trade)
                await self._event_bus.publish(
                    Event(
                        topic=TOPIC_TRAILING_SL_UPDATE,
                        payload={**trade.to_dict()},
                        source_module=self.module_id,
                    )
                )
        return trade

    async def _apply_exit_intelligence(
        self,
        trade: Trade,
        current_price: float,
        **intel_kwargs: Any,
    ) -> Trade | None:
        """Consult PositionManager. Returns Trade if fully closed, else None."""
        from aitos.intelligence.exit_intelligence import ExitAction

        assert self._position_manager is not None
        pos_action = self._position_manager.evaluate(
            trade=trade,
            current_price=current_price,
            **intel_kwargs,
        )

        # Stash last action so TP path can honour HOLD
        trade._last_eie_action = pos_action.action.value  # type: ignore[attr-defined]

        await self._event_bus.publish(
            Event(
                topic=TOPIC_EXIT_DECISION,
                payload={
                    "trade_id": trade.trade_id,
                    **pos_action.to_dict(),
                },
                source_module=self.module_id,
            )
        )
        action_type = str(pos_action.action.value).upper()
        await self._record_decision(
            f"{trade.trade_id}:EIE:{time.time_ns()}",
            entity_id=self._trade_attribution_id(trade),
            symbol=trade.symbol,
            side=trade.side.value,
            decision_type=action_type,
            accepted=action_type != "HOLD",
            reason=str(getattr(pos_action, "reason", action_type.lower())),
            payload=pos_action.to_dict(),
        )

        if pos_action.action == ExitAction.EXIT:
            return await self._trigger_exit(
                trade,
                current_price,
                pos_action.reason,
                TOPIC_EXIT_DECISION,
            )

        if pos_action.action == ExitAction.MANAGE:
            if pos_action.reduce_fraction > 0:
                allowed, guard_reason = self._partial_exit_allowed(
                    trade, pos_action.reduce_fraction
                )
                if allowed:
                    await self._partial_close(
                        trade, current_price, pos_action.reduce_fraction
                    )
                    self._last_partial_exit_at[trade.trade_id] = time.monotonic()
                else:
                    logger.info(
                        "partial exit suppressed by execution guard",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "reason": guard_reason,
                                "partial_exit_count": len(trade.partial_exits),
                                "remaining_position_usd": trade.position_size_usd,
                            }
                        },
                    )
            if pos_action.new_stop_price is not None:
                # Only tighten (never loosen) — already enforced in PositionManager
                trade.sl_price = pos_action.new_stop_price
                if self._position_manager is not None:
                    current_emergency = self._emergency_stop_price(trade)
                    if (
                        trade.side == TradeSide.LONG
                        and pos_action.new_stop_price > current_emergency
                    ) or (
                        trade.side == TradeSide.SHORT
                        and pos_action.new_stop_price < current_emergency
                    ):
                        trade.emergency_sl_price = pos_action.new_stop_price
                trade.updated_at = utc_now_iso()
                if self._use_exchange_side_stops:
                    await self._replace_exchange_side_stop_loss(trade)
                await self._event_bus.publish(
                    Event(
                        topic=TOPIC_POSITION_UPDATED,
                        payload={
                            **trade.to_dict(),
                            "reason": "eie_stop_tighten",
                            "new_stop": pos_action.new_stop_price,
                        },
                        source_module=self.module_id,
                    )
                )
        if pos_action.target_extension is not None and trade.take_profit_levels:
            decision = pos_action.target_extension
            current_runner_target = float(trade.take_profit_levels[-1])
            contract = TakeProfitExecutionContract(intelligent=True)
            approved_extension = bool(
                getattr(decision, "approved", False)
                and contract.can_extend(
                    trade.side, current_runner_target, float(decision.target_price)
                )
            )
            if approved_extension:
                trade.take_profit_levels[-1] = decision.target_price
                trade.updated_at = utc_now_iso()
                await self._event_bus.publish(
                    Event(
                        topic=TOPIC_POSITION_UPDATED,
                        payload={
                            **trade.to_dict(),
                            "reason": "path_planner_runner_target_guard",
                            "target_extension": decision.to_dict(),
                        },
                        source_module=self.module_id,
                    )
                )

        # HOLD → retain the position; only independent protection logic follows
        return None

    async def apply_cost_adjustment(
        self,
        trade_id: str,
        *,
        adjustment_id: str,
        kind: str,
        amount_usd: float,
    ) -> Trade:
        """Apply an idempotent realized fee or funding adjustment.

        Fee amounts are positive costs; funding is signed (positive means
        received, negative means paid). Producers must supply stable IDs.
        """
        if not adjustment_id or not adjustment_id.strip():
            raise ValueError("adjustment_id must not be empty")
        if kind not in {"fee", "funding"}:
            raise ValueError("kind must be 'fee' or 'funding'")
        if not math.isfinite(amount_usd) or (kind == "fee" and amount_usd < 0):
            raise ValueError("cost adjustment must be finite; fees cannot be negative")
        trade = self._open_trades.get(trade_id)
        if trade is None:
            trade = next(
                (item for item in self._closed_trades if item.trade_id == trade_id),
                None,
            )
        if trade is None:
            raise TradeNotFoundError(f"No trade with id '{trade_id}'")
        if adjustment_id in trade.cost_adjustments:
            return trade

        trade.cost_adjustments[adjustment_id] = {
            "kind": kind,
            "amount_usd": float(amount_usd),
            "recorded_at": utc_now_iso(),
        }
        if kind == "fee":
            trade.fees_paid_usd += float(amount_usd)
        else:
            trade.funding_pnl_usd += float(amount_usd)
        if (
            trade.gross_realized_pnl_usd != 0.0
            or trade.state == TradeLifecycleState.POSITION_CLOSED
        ):
            trade.pnl = round(
                trade.gross_realized_pnl_usd
                - trade.fees_paid_usd
                + trade.funding_pnl_usd,
                8,
            )
            initial_notional = trade.position_size_usd + sum(
                exit.size_usd for exit in trade.partial_exits
            )
            trade.pnl_percent = (
                round(trade.pnl / initial_notional * 100.0, 8)
                if initial_notional > 0
                else 0.0
            )
        if trade.state == TradeLifecycleState.POSITION_CLOSED:
            gateway = getattr(self, "_aitos_capital_gateway", None)
            capital_objective = trade.agent_consensus.get("capital_objective", {})
            predicted_loss_probability = (
                capital_objective.get("predicted_loss_probability")
                if isinstance(capital_objective, dict)
                else None
            )
            if gateway is not None and predicted_loss_probability is not None:
                try:
                    await asyncio.to_thread(
                        gateway.calibrator.revise,
                        float(predicted_loss_probability),
                        trade.pnl <= 0.0,
                        observation_id=trade.trade_id,
                    )
                except Exception:
                    logger.exception(
                        "capital probability calibration revision failed",
                        extra={"aitos_extra": {"trade_id": trade.trade_id}},
                    )
        trade.updated_at = utc_now_iso()
        await self._event_bus.publish(
            Event(
                topic=TOPIC_POSITION_UPDATED,
                payload={
                    **trade.to_dict(),
                    "reason": "trade_cost_adjustment",
                    "adjustment_id": adjustment_id,
                    "adjustment_kind": kind,
                },
                source_module=self.module_id,
            )
        )
        return trade

    async def close_trade(self, trade_id: str, exit_price: float, reason: str) -> Trade:
        """Measure close transitions while leaving hard exit logic authoritative."""
        import time

        started = time.perf_counter()
        trade = self._open_trades.get(trade_id)
        symbol = str(getattr(trade, "symbol", "") or "")
        try:
            from aitos.forensics.data_flow_hooks import (
                record_error,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.TRADE_LIFECYCLE.value, symbol=symbol)
        except Exception:
            pass
        try:
            result = await self._close_trade_impl(trade_id, exit_price, reason)
        except Exception as exc:
            if trade is not None:
                await self._record_decision(
                    f"{trade_id}:CLOSE_EXECUTION_FAILURE:{time.time_ns()}",
                    entity_id=self._trade_attribution_id(trade),
                    symbol=symbol or "UNKNOWN",
                    side=trade.side.value,
                    decision_type="EXECUTION_FAILURE",
                    accepted=False,
                    reason=f"close_execution_failed:{type(exc).__name__}",
                    payload={
                        "trade_id": trade_id,
                        "error_type": type(exc).__name__,
                    },
                )
            try:
                record_error(
                    PipelineStage.TRADE_LIFECYCLE.value,
                    symbol=symbol,
                    issue="processing_error",
                )
            except Exception:
                pass
            raise
        try:
            record_output(
                PipelineStage.TRADE_LIFECYCLE.value,
                symbol=symbol,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception:
            pass
        return result

    async def finalize_confirmed_flat_close(
        self, trade_id: str, receipt: Any, client_id: str
    ) -> Trade:
        """Apply an already journaled switch close, never submit it twice."""
        async with self._execution_locks.setdefault(trade_id, asyncio.Lock()):
            self._assert_execution_unlatched(trade_id)
            self._require_execution_writer()
            trade = self._open_trades.get(trade_id)
            if trade is None:
                raise TradeNotFoundError(trade_id)
            if not (receipt.confirmed and receipt.position_flat and client_id):
                raise ValueError("confirmed venue-flat close receipt required")
            self._confirmed_reductions[trade_id] = (
                client_id,
                OrderResult(
                    receipt.order_id,
                    trade.symbol,
                    trade.side,
                    receipt.filled_quantity,
                    receipt.fill_price,
                ),
            )
            try:
                return await self._close_trade_unlocked(
                    trade_id, receipt.fill_price, "capital_rebalance"
                )
            finally:
                self._confirmed_reductions.pop(trade_id, None)

    async def _close_trade_impl(
        self, trade_id: str, exit_price: float, reason: str
    ) -> Trade:
        if not self._order_executor.requires_exchange_execution:
            return await self._close_trade_unlocked(trade_id, exit_price, reason)
        async with self._execution_locks.setdefault(trade_id, asyncio.Lock()):
            self._assert_execution_unlatched(trade_id)
            return await self._close_trade_unlocked(trade_id, exit_price, reason)

    async def _close_trade_unlocked(
        self, trade_id: str, exit_price: float, reason: str
    ) -> Trade:
        self._require_initialized()
        if not _valid_market_price(exit_price):
            raise ValueError(
                f"exit_price must be a finite positive number, got {exit_price!r}"
            )
        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        if self._order_executor.requires_exchange_execution:
            execution = await self._execute_live_reduction(
                trade, trade.quantity, exit_price
            )
            exit_price = execution.fill_price
            if execution.filled_quantity < trade.quantity * (1 - 1e-9):
                trade.state = TradeLifecycleState.POSITION_OPENED
                await self._partial_close_unlocked(
                    trade,
                    exit_price,
                    execution.filled_quantity / trade.quantity,
                    execution_result=execution,
                )
                trade.state = TradeLifecycleState.POSITION_OPENED
                return trade
        # Cancel exchange-side resting orders before committing the local
        # close transition. If cancellation fails, keep the trade registered
        # as open so monitoring/reconciliation can retry instead of silently
        # orphaning an exchange position or leaving stale orders behind.
        if self._use_exchange_side_stops:
            await self._cancel_resting_orders(trade)
        self._open_trades.pop(trade_id, None)
        direction = 1 if trade.side == TradeSide.LONG else -1
        pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * direction
        realized_on_remaining = trade.position_size_usd * pnl_pct
        trade.gross_realized_pnl_usd += realized_on_remaining
        trade.pnl = round(
            trade.gross_realized_pnl_usd - trade.fees_paid_usd + trade.funding_pnl_usd,
            8,
        )
        initial_position_size_usd = trade.position_size_usd + sum(
            pe.size_usd for pe in trade.partial_exits
        )
        trade.pnl_percent = (
            round((trade.pnl / initial_position_size_usd) * 100, 4)
            if initial_position_size_usd > 0
            else 0.0
        )
        trade.exit_price = exit_price
        trade.exit_time = utc_now_iso()
        trade.exit_reason = reason
        trade.state = TradeLifecycleState.POSITION_CLOSED
        trade.updated_at = trade.exit_time
        ere_observations = trade.agent_consensus.get("ere_observations", [])
        if isinstance(ere_observations, list):
            for observation in ere_observations:
                if not isinstance(observation, dict):
                    continue
                try:
                    decision_price = float(observation["price"])
                    decision_at = datetime.fromisoformat(
                        str(observation["decision_at"]).replace("Z", "+00:00")
                    )
                    exit_at = datetime.fromisoformat(
                        str(trade.exit_time).replace("Z", "+00:00")
                    )
                    if (
                        decision_price <= 0
                        or decision_at.tzinfo is None
                        or exit_at <= decision_at
                    ):
                        continue
                    realized_move_pct = (
                        (exit_price / decision_price - 1.0) * direction * 100.0
                    )
                    observation["realized_remaining_edge_pct"] = (
                        realized_move_pct
                        - float(observation.get("estimated_cost_pct", 0.0))
                    )
                    observation["outcome_at"] = trade.exit_time
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue
        self._closed_trades.append(trade)
        if self._order_executor.requires_exchange_execution:
            await self._commit_live_execution(trade)
        await self._resolve_decision_outcomes(trade)
        gateway = getattr(self, "_aitos_capital_gateway", None)
        if gateway is not None:
            try:
                gateway.release(trade.symbol)
            except Exception:
                logger.exception(
                    "capital reservation release failed after position close",
                    extra={"aitos_extra": {"trade_id": trade.trade_id}},
                )
        capital_objective = trade.agent_consensus.get("capital_objective", {})
        if not isinstance(capital_objective, dict):
            capital_objective = {}
        predicted_loss_probability = capital_objective.get("predicted_loss_probability")
        if gateway is not None and predicted_loss_probability is not None:
            try:
                await asyncio.to_thread(
                    gateway.calibrator.observe,
                    float(predicted_loss_probability),
                    trade.pnl <= 0.0,
                    observation_id=trade.trade_id,
                )
            except Exception:
                logger.exception(
                    "capital probability calibration update failed",
                    extra={"aitos_extra": {"trade_id": trade.trade_id}},
                )
        if self._position_manager is not None:
            self._position_manager.clear_trade(trade.trade_id, symbol=trade.symbol)
        self._last_intensive_analysis_at.pop(trade.trade_id, None)
        self._last_partial_exit_at.pop(trade.trade_id, None)
        self._soft_stop_breached.discard(trade.trade_id)
        self._tp_boundary_breached.discard(trade.trade_id)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_POSITION_CLOSED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        try:
            await asyncio.to_thread(self._record_statistical_outcome, trade)
        except Exception:
            logger.exception(
                "statistical shadow outcome persistence failed",
                extra={"aitos_extra": {"trade_id": trade.trade_id}},
            )
        self._last_event_time = trade.updated_at
        return trade

    async def reconcile_trade(self, trade_id: str) -> Trade:
        self._require_initialized()
        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        if (
            not self._use_exchange_side_stops
            or trade.state != TradeLifecycleState.POSITION_OPENED
        ):
            return trade
        if trade.sl_order_id:
            status = await self._order_executor.get_resting_order_status(
                trade.symbol, trade.sl_order_id
            )
            if status in {"FILLED", "PARTIALLY_FILLED_TERMINAL"}:
                # Remember the exchange-filled order but retain its ID until
                # close succeeds, so a failed cancellation of another order
                # cannot lose the reconciliation signal.
                filled_ids = set(
                    getattr(trade, "_aitos_filled_resting_order_ids", set())
                )
                filled_ids.add(trade.sl_order_id)
                trade._aitos_filled_resting_order_ids = filled_ids
                exchange_exit_price = (
                    self._emergency_stop_price(trade)
                    if self._position_manager is not None
                    else trade.sl_price
                )
                return await self.close_trade(
                    trade.trade_id, exchange_exit_price, "sl_triggered_exchange"
                )
        if trade.tp_order_ids:
            status = await self._order_executor.get_resting_order_status(
                trade.symbol, trade.tp_order_ids[0]
            )
            if status in {"FILLED", "PARTIALLY_FILLED_TERMINAL"}:
                trigger_price = (
                    trade.take_profit_levels[0]
                    if trade.take_profit_levels
                    else trade.tp_price
                )
                if len(trade.take_profit_levels) > 1:
                    # A filled partial TP is already consumed by the exchange.
                    if self._order_executor.requires_exchange_execution:
                        trade._aitos_filled_resting_order_ids = {trade.tp_order_ids[0]}
                    await self._partial_close(
                        trade, trigger_price, DEFAULT_PARTIAL_CLOSE_FRACTION
                    )
                    if not self._order_executor.requires_exchange_execution:
                        trade.take_profit_levels.pop(0)
                        if trade.tp_order_ids:
                            trade.tp_order_ids.pop(0)
                    return trade
                filled_ids = set(
                    getattr(trade, "_aitos_filled_resting_order_ids", set())
                )
                filled_ids.add(trade.tp_order_ids[0])
                trade._aitos_filled_resting_order_ids = filled_ids
                return await self.close_trade(
                    trade.trade_id, trigger_price, "tp_triggered_exchange"
                )
        return trade

    async def _place_exchange_side_stops(self, trade: Trade) -> None:
        exchange_stop_price = (
            self._emergency_stop_price(trade)
            if self._position_manager is not None
            else trade.sl_price
        )
        sl_result = await self._order_executor.place_stop_loss_order(
            trade.symbol, trade.side, trade.quantity, exchange_stop_price
        )
        if sl_result.success:
            trade.sl_order_id = sl_result.order_id
            if self._order_executor.requires_exchange_execution:
                trade.updated_at = utc_now_iso()
                await self._execution_state_writer(trade)
                self._order_executor.acknowledge_protection(sl_result, trade.trade_id)
        else:
            if self._order_executor.requires_exchange_execution:
                raise ExecutionUncertainError(
                    trade.symbol, trade.trade_id, "protective stop rejected"
                )
            logger.error(
                "failed to place exchange-side stop loss",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "error": sl_result.error,
                    }
                },
            )
        remaining_qty = trade.quantity
        # A resting exchange TP would bypass an EIE HOLD decision. EIE-managed
        # positions therefore monitor TP destinations locally and let EIE
        # arbitrate full exits; legacy positions retain their exchange TPs.
        levels = [] if self._position_manager is not None else trade.take_profit_levels
        for i, level_price in enumerate(levels):
            leg_qty = (
                remaining_qty
                if i == len(levels) - 1
                else remaining_qty * DEFAULT_PARTIAL_CLOSE_FRACTION
            )
            remaining_qty -= leg_qty
            tp_result = await self._order_executor.place_take_profit_order(
                trade.symbol, trade.side, leg_qty, level_price
            )
            if tp_result.success:
                trade.tp_order_ids.append(tp_result.order_id)
                if self._order_executor.requires_exchange_execution:
                    trade.updated_at = utc_now_iso()
                    await self._execution_state_writer(trade)
                    self._order_executor.acknowledge_protection(
                        tp_result, trade.trade_id
                    )
            else:
                if self._order_executor.requires_exchange_execution:
                    raise ExecutionUncertainError(
                        trade.symbol, trade.trade_id, "protective target rejected"
                    )
                logger.error(
                    "failed to place exchange-side take profit",
                    extra={
                        "aitos_extra": {
                            "trade_id": trade.trade_id,
                            "error": tp_result.error,
                        }
                    },
                )
        if self._order_executor.requires_exchange_execution:
            trade.updated_at = utc_now_iso()
            await self._execution_state_writer(trade)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_EXCHANGE_STOPS_PLACED,
                payload={**trade.to_dict()},
                source_module=self.module_id,
            )
        )

    async def _replace_exchange_side_stop_loss(self, trade: Trade) -> None:
        if self._order_executor.requires_exchange_execution:
            self._require_execution_writer()
            await self._refresh_live_protection(trade)
            await self._execution_position_verifier()
            return
        if trade.sl_order_id:
            await self._order_executor.cancel_resting_order(
                trade.symbol, trade.sl_order_id
            )
        exchange_stop_price = (
            self._emergency_stop_price(trade)
            if self._position_manager is not None
            else trade.sl_price
        )
        result = await self._order_executor.place_stop_loss_order(
            trade.symbol, trade.side, trade.quantity, exchange_stop_price
        )
        trade.sl_order_id = result.order_id if result.success else None

    async def _cancel_resting_orders(self, trade: Trade) -> None:
        # Cancel take-profit orders first and keep the protective stop active
        # until every TP cancellation succeeds. Remove IDs only after confirmed
        # cancellation so a later retry can resume from the remaining orders.
        filled_ids = set(getattr(trade, "_aitos_filled_resting_order_ids", set()))
        pending_stops = trade.agent_consensus.get("_pending_stop_cancellations", [])
        for order_id in list(pending_stops):
            await self._order_executor.cancel_resting_order(trade.symbol, order_id)
            pending_stops.remove(order_id)
        for order_id in list(trade.tp_order_ids):
            if order_id not in filled_ids:
                await self._order_executor.cancel_resting_order(trade.symbol, order_id)
            trade.tp_order_ids.remove(order_id)
        if trade.sl_order_id:
            if trade.sl_order_id not in filled_ids:
                await self._order_executor.cancel_resting_order(
                    trade.symbol, trade.sl_order_id
                )
            trade.sl_order_id = None
        if hasattr(trade, "_aitos_filled_resting_order_ids"):
            del trade._aitos_filled_resting_order_ids

    def _project_portfolio(
        self,
        portfolio: PortfolioState,
        opportunity: Opportunity,
        sizing: PositionSizeResult,
    ) -> PortfolioState:
        candidate = PositionExposure(
            symbol=opportunity.symbol,
            notional_usd=sizing.position_size_usd,
            leverage=sizing.leverage,
            side=opportunity.side.value,
            risk_amount_usd=sizing.risk_amount_usd,
        )
        return PortfolioState(
            equity_usd=portfolio.equity_usd,
            peak_equity_usd=portfolio.peak_equity_usd,
            positions=(*portfolio.positions, candidate),
            daily_pnl_pct=portfolio.daily_pnl_pct,
            weekly_pnl_pct=portfolio.weekly_pnl_pct,
            volatility_percentile=portfolio.volatility_percentile,
            regime=portfolio.regime,
            max_pairwise_correlation=portfolio.max_pairwise_correlation,
            api_error_rate_pct=portfolio.api_error_rate_pct,
            api_latency_ms=portfolio.api_latency_ms,
            data_freshness_seconds=portfolio.data_freshness_seconds,
            model_accuracy=portfolio.model_accuracy,
            timestamp=portfolio.timestamp,
        )

    async def _validate_and_open(
        self, opportunity: Opportunity, portfolio: PortfolioState
    ) -> Trade:
        from aitos.risk.position_sizing import calculate_position_size
        from aitos.risk.sector import sector_for_symbol

        live_execution = self._order_executor.requires_exchange_execution
        if live_execution:
            self._require_execution_writer()
            if getattr(self, "_execution_recovery_required", False) or any(
                t.agent_consensus.get("_pending_stop_cancellations")
                for t in self._open_trades.values()
            ):
                raise ExecutionRecoveryRequired(
                    opportunity.symbol,
                    opportunity.opportunity_id,
                    "unresolved protection/execution blocks entry",
                )
            if getattr(self, "_account_reconciliation_error", False):
                raise ExecutionRecoveryRequired(
                    opportunity.symbol,
                    opportunity.opportunity_id,
                    "account exposure mismatch blocks entry",
                )
            if not self._use_exchange_side_stops:
                raise RuntimeError("live entry requires exchange-side protection")
            if any(t.symbol == opportunity.symbol for t in self._open_trades.values()):
                return await self._reject(
                    opportunity, "local position already open for symbol"
                )
        risk_score = (
            self._risk_engine.last_assessment.total
            if self._risk_engine.last_assessment
            else 0.0
        )
        candidate_sector = sector_for_symbol(opportunity.symbol)
        existing_sector_notional = sum(
            p.notional_usd for p in portfolio.positions if p.sector == candidate_sector
        )
        # Size against the actual catastrophic boundary when EIE is enabled,
        # not the closer soft thesis stop. This preserves the configured account
        # risk even though a valid thesis may be held beyond the soft stop.
        sizing_stop_price = opportunity.stop_loss_price
        if self._position_manager is not None:
            initial_distance = abs(
                opportunity.entry_price - opportunity.stop_loss_price
            )
            emergency_multiple = float(self._tuning.positions.emergency_stop_r_multiple)
            sizing_stop_price = (
                opportunity.entry_price - initial_distance * emergency_multiple
                if opportunity.side == TradeSide.LONG
                else opportunity.entry_price + initial_distance * emergency_multiple
            )
            if not _valid_market_price(sizing_stop_price):
                return await self._reject(
                    opportunity,
                    "invalid emergency stop price for EIE-managed position",
                )
        capital_objective = opportunity.agent_consensus.get("capital_objective", {})
        approved_risk_budget = capital_objective.get("risk_budget_usd")
        if approved_risk_budget is not None:
            approved_risk_budget = float(approved_risk_budget)
            if not math.isfinite(approved_risk_budget) or approved_risk_budget <= 0:
                return await self._reject(
                    opportunity, "capital_objective: invalid approved risk budget"
                )

        sizing = calculate_position_size(
            equity_usd=portfolio.equity_usd,
            entry_price=opportunity.entry_price,
            stop_loss_price=sizing_stop_price,
            risk_limits=self._risk_engine.limits,
            risk_score=risk_score,
            volatility_percentile=portfolio.volatility_percentile,
            correlation_penalty=portfolio.max_pairwise_correlation,
            existing_sector_notional_usd=existing_sector_notional,
            sector_limit_pct=self._risk_engine.limits.max_sector_exposure_pct,
        )

        # Apply the streak multiplier after leverage/sector caps so the
        # realized executable risk is actually reduced by the configured
        # percentage, even when a portfolio exposure cap was already binding.
        risk_multiplier = float(capital_objective.get("risk_reduction_multiplier", 1.0))
        if not math.isfinite(risk_multiplier) or not 0.0 < risk_multiplier <= 1.0:
            return await self._reject(
                opportunity, "capital_objective: invalid risk reduction multiplier"
            )
        if risk_multiplier < 1.0:
            scaled_notional = (
                math.floor(sizing.position_size_usd * risk_multiplier * 100.0) / 100.0
            )
            stop_risk_rate = (
                abs(opportunity.entry_price - sizing_stop_price)
                / opportunity.entry_price
            )
            sizing = PositionSizeResult(
                position_size_usd=scaled_notional,
                leverage=sizing.leverage,
                risk_amount_usd=round(scaled_notional * stop_risk_rate, 2),
                rationale=(
                    f"{sizing.rationale}; loss-streak multiplier={risk_multiplier:.3f}"
                ),
            )

        # Enforce the gateway's absolute USD risk budget after all sizing caps.
        if approved_risk_budget is not None:
            stop_risk_rate = (
                abs(opportunity.entry_price - sizing_stop_price)
                / opportunity.entry_price
            )
            max_notional = (
                math.floor((approved_risk_budget / stop_risk_rate) * 100.0) / 100.0
            )
            if sizing.position_size_usd > max_notional:
                capped_notional = max(0.0, max_notional)
                sizing = PositionSizeResult(
                    position_size_usd=capped_notional,
                    leverage=sizing.leverage,
                    risk_amount_usd=round(capped_notional * stop_risk_rate, 2),
                    rationale=(
                        f"{sizing.rationale}; capital risk cap applied "
                        f"({approved_risk_budget:.8f} USD)"
                    ),
                )

        statistical_risk_assessment = None
        statistical_forecast = opportunity.agent_consensus.get("statistical_forecast")
        if statistical_forecast is not None:
            try:
                statistical_risk_assessment = assess_statistical_risk_forecast(
                    statistical_forecast,
                    symbol=opportunity.symbol,
                    side=opportunity.side.value,
                    horizon=opportunity.timeframe,
                )
            except (TypeError, ValueError) as exc:
                return await self._reject(
                    opportunity, f"statistical_risk_overlay: {exc}"
                )
            if statistical_risk_assessment.size_multiplier < 1.0:
                stop_risk_rate = (
                    abs(opportunity.entry_price - sizing_stop_price)
                    / opportunity.entry_price
                )
                reduced_notional = (
                    math.floor(
                        sizing.position_size_usd
                        * statistical_risk_assessment.size_multiplier
                        * 100.0
                    )
                    / 100.0
                )
                sizing = PositionSizeResult(
                    position_size_usd=reduced_notional,
                    leverage=sizing.leverage,
                    risk_amount_usd=round(reduced_notional * stop_risk_rate, 2),
                    rationale=(
                        f"{sizing.rationale}; statistical risk multiplier="
                        f"{statistical_risk_assessment.size_multiplier:.4f}"
                    ),
                )
        if sizing.position_size_usd <= 0.0:
            return await self._reject(
                opportunity, f"sector exposure cap exhausted for {candidate_sector}"
            )
        projected_portfolio = self._project_portfolio(portfolio, opportunity, sizing)
        projected_hard_breaches = [
            b
            for b in self._risk_engine.check_limits(projected_portfolio)
            if b.is_hard_cap
        ]
        if projected_hard_breaches:
            return await self._reject(
                opportunity,
                f"projected hard limit breach: {projected_hard_breaches[0].message}",
            )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_ENTRY_SIGNAL,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "sizing_rationale": sizing.rationale,
                },
                source_module=self.module_id,
            )
        )
        quantity = sizing.position_size_usd / opportunity.entry_price
        # Bind both durable ledgers to one stable logical opportunity identity.
        # The client ID suffix must remain the actual trade ID for orphan checks.
        trade_id = hashlib.sha256(opportunity.opportunity_id.encode()).hexdigest()[:24]
        order_result = await self._order_executor.submit_order(
            OrderRequest(
                symbol=opportunity.symbol,
                side=opportunity.side,
                quantity=quantity,
                reference_price=opportunity.entry_price,
                client_order_id=f"aitos-entry-{trade_id}",
            )
        )
        if not order_result.success and order_result.filled_quantity <= 0.0:
            return await self._reject(
                opportunity, f"order submission failed: {order_result.error}"
            )
        if not live_execution and (
            not _valid_market_price(order_result.filled_quantity)
            or not _valid_market_price(order_result.fill_price)
        ):
            return await self._reject(
                opportunity,
                "order accepted without a valid confirmed fill; venue reconciliation required",
            )
        if (
            not _valid_market_price(order_result.filled_quantity)
            or not _valid_market_price(order_result.fill_price)
            or order_result.filled_quantity > quantity * (1 + 1e-9)
        ):
            raise ExecutionUncertainError(
                opportunity.symbol,
                order_result.order_id,
                "invalid entry execution evidence",
            )
        actual_filled_notional = order_result.filled_quantity * order_result.fill_price
        fill_ratio = actual_filled_notional / sizing.position_size_usd
        if not math.isfinite(fill_ratio) or fill_ratio <= 0.0:
            return await self._reject(
                opportunity,
                "venue fill produced an invalid actual position notional",
            )
        execution_error = order_result.error
        if not order_result.success and not execution_error:
            execution_error = (
                "adapter reported failure despite a confirmed venue fill; "
                "manual execution reconciliation required"
            )
        sizing = PositionSizeResult(
            position_size_usd=actual_filled_notional,
            leverage=sizing.leverage,
            risk_amount_usd=round(sizing.risk_amount_usd * fill_ratio, 2),
            rationale=(
                f"{sizing.rationale}; reconciled to actual venue fill "
                f"({order_result.filled_quantity:.12g} @ "
                f"{order_result.fill_price:.12g})"
            ),
        )
        trade = Trade(
            trade_id=trade_id,
            symbol=opportunity.symbol,
            side=opportunity.side,
            entry_price=order_result.fill_price,
            quantity=order_result.filled_quantity,
            leverage=sizing.leverage,
            position_size_usd=(
                order_result.filled_quantity * order_result.fill_price
                if live_execution
                else sizing.position_size_usd
            ),
            risk_amount_usd=(
                abs(order_result.fill_price - sizing_stop_price)
                * order_result.filled_quantity
                if live_execution
                else sizing.risk_amount_usd
            ),
            strategy_id=opportunity.strategy_id,
            agent_consensus={
                **opportunity.agent_consensus,
                **(
                    {
                        "statistical_risk_assessment": {
                            "size_multiplier": statistical_risk_assessment.size_multiplier,
                            "volatility_ratio": statistical_risk_assessment.volatility_ratio,
                            "tail_probability": statistical_risk_assessment.tail_probability,
                            "expected_shortfall": statistical_risk_assessment.expected_shortfall,
                            "reasons": list(statistical_risk_assessment.reasons),
                        }
                    }
                    if statistical_risk_assessment is not None
                    else {}
                ),
            },
            explanation=opportunity.rationale,
            sl_price=opportunity.stop_loss_price,
            tp_price=(
                opportunity.take_profit_levels[0]
                if opportunity.take_profit_levels
                else opportunity.entry_price
            ),
            take_profit_levels=list(opportunity.take_profit_levels),
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time=utc_now_iso(),
            trailing_sl_enabled=opportunity.trailing_sl_enabled,
            breakeven_at_r_multiple=opportunity.breakeven_at_r_multiple,
            regime=opportunity.regime,
            emergency_sl_price=(
                (
                    (
                        order_result.fill_price
                        - abs(order_result.fill_price - opportunity.stop_loss_price)
                        * float(self._tuning.positions.emergency_stop_r_multiple)
                    )
                    if opportunity.side == TradeSide.LONG
                    else (
                        order_result.fill_price
                        + abs(order_result.fill_price - opportunity.stop_loss_price)
                        * float(self._tuning.positions.emergency_stop_r_multiple)
                    )
                )
                if self._position_manager is not None
                else None
            ),
        )
        if live_execution:
            actual_stop = (
                trade.emergency_sl_price
                if self._position_manager is not None
                else trade.sl_price
            )
            trade.risk_amount_usd = (
                abs(trade.entry_price - actual_stop) * trade.quantity
            )
        self._open_trades[trade.trade_id] = trade
        if live_execution:
            client_id = f"aitos-entry-{trade.trade_id}"
            self._execution_faults[trade.trade_id] = client_id
            # Install durable ownership before optional telemetry can fail.
            await self._execution_state_writer(trade)
            await self._place_exchange_side_stops(trade)
            await self._commit_live_execution(trade)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_ORDER_SUBMITTED,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "quantity": quantity,
                },
                source_module=self.module_id,
            )
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_ORDER_FILLED,
                payload={
                    "order_id": order_result.order_id,
                    "fill_price": order_result.fill_price,
                },
                source_module=self.module_id,
            )
        )
        candidate_id = trade.agent_consensus.get("candidate_id")
        if candidate_id:
            try:
                bound = await asyncio.to_thread(
                    self._financial_ml_pipeline().mark_executed,
                    str(candidate_id),
                    trade.trade_id,
                )
                if not bound:
                    logger.error(
                        "filled trade could not bind its Financial ML candidate",
                        extra={
                            "aitos_extra": {
                                "candidate_id": candidate_id,
                                "trade_id": trade.trade_id,
                            }
                        },
                    )
            except Exception:
                logger.exception(
                    "financial ML candidate execution binding failed",
                    extra={
                        "aitos_extra": {
                            "candidate_id": candidate_id,
                            "trade_id": trade.trade_id,
                        }
                    },
                )
        await self._record_decision(
            f"{trade.trade_id}:ENTRY",
            entity_id=self._trade_attribution_id(trade),
            symbol=trade.symbol,
            side=trade.side.value,
            decision_type="ENTRY",
            accepted=True,
            reason="entry_order_filled_and_position_opened",
            payload={
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
                "position_size_usd": trade.position_size_usd,
                "risk_amount_usd": trade.risk_amount_usd,
                "strategy_id": trade.strategy_id,
                "candidate_id": trade.agent_consensus.get("candidate_id"),
                "world_intelligence_shadow": trade.agent_consensus.get(
                    "world_intelligence_shadow"
                ),
                "statistical_risk_assessment": trade.agent_consensus.get(
                    "statistical_risk_assessment"
                ),
            },
            decision_at=trade.entry_time,
        )
        if execution_error:
            await self._record_decision(
                f"{trade.trade_id}:EXECUTION_FAILURE",
                entity_id=self._trade_attribution_id(trade),
                symbol=trade.symbol,
                side=trade.side.value,
                decision_type="EXECUTION_FAILURE",
                accepted=False,
                reason="partial_fill_or_order_state_requires_reconciliation",
                payload={
                    "order_id": order_result.order_id,
                    "filled_quantity": order_result.filled_quantity,
                    "fill_price": order_result.fill_price,
                    "execution_error": execution_error,
                },
            )
            await self._event_bus.publish(
                Event(
                    topic=TOPIC_EXECUTION_RECONCILIATION_REQUIRED,
                    payload={
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "order_id": order_result.order_id,
                        "filled_quantity": order_result.filled_quantity,
                        "error": execution_error,
                    },
                    source_module=self.module_id,
                )
            )
        if self._position_manager is not None:
            register_entry_thesis = getattr(
                self._position_manager, "register_entry_thesis", None
            )
            if callable(register_entry_thesis):
                try:
                    entry_context = (
                        self._market_context_provider.get_context(
                            trade.symbol
                        ).as_kwargs()
                        if self._market_context_provider is not None
                        else {}
                    )
                    register_entry_thesis(trade, **entry_context)
                    logger.info(
                        "entry thesis captured",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "has_entry_market_context": bool(entry_context),
                                "entry_thesis_captured": trade.entry_thesis is not None,
                            }
                        },
                    )
                except Exception:
                    # Keep the position open but make the degraded entry snapshot
                    # visible; PositionManager has a deterministic fallback path.
                    logger.exception(
                        "entry thesis capture failed",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                            }
                        },
                    )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_POSITION_OPENED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        self._last_event_time = trade.entry_time
        if self._use_exchange_side_stops and not live_execution:
            await self._place_exchange_side_stops(trade)
        if statistical_risk_assessment is not None:
            try:
                await asyncio.to_thread(self._record_statistical_forecast, trade)
            except Exception:
                logger.exception(
                    "statistical shadow forecast persistence failed",
                    extra={"aitos_extra": {"trade_id": trade.trade_id}},
                )
        return trade

    async def _reject(self, opportunity: Opportunity, reason: str) -> Trade:
        candidate_id = opportunity.agent_consensus.get("candidate_id")
        if candidate_id:
            try:
                await asyncio.to_thread(
                    self._financial_ml_pipeline().reject_candidate,
                    str(candidate_id),
                    reason,
                )
            except Exception:
                logger.exception(
                    "financial ML rejected candidate transition failed",
                    extra={"aitos_extra": {"candidate_id": candidate_id}},
                )
        trade = Trade(
            trade_id=new_trade_id(),
            symbol=opportunity.symbol,
            side=opportunity.side,
            entry_price=opportunity.entry_price,
            quantity=0.0,
            leverage=0.0,
            position_size_usd=0.0,
            risk_amount_usd=0.0,
            strategy_id=opportunity.strategy_id,
            agent_consensus=opportunity.agent_consensus,
            explanation=opportunity.rationale,
            sl_price=opportunity.stop_loss_price,
            tp_price=(
                opportunity.take_profit_levels[0]
                if opportunity.take_profit_levels
                else opportunity.entry_price
            ),
            take_profit_levels=list(opportunity.take_profit_levels),
            state=TradeLifecycleState.REJECTED,
            entry_time=utc_now_iso(),
            rejection_reason=reason,
        )
        failure_type = (
            "EXECUTION_FAILURE"
            if reason.startswith("order submission failed:")
            else "REJECTION"
        )
        await self._record_decision(
            f"{trade.trade_id}:{failure_type}",
            entity_id=self._trade_attribution_id(trade),
            symbol=trade.symbol,
            side=trade.side.value,
            decision_type=failure_type,
            accepted=False,
            reason=reason,
            payload={
                "strategy_id": trade.strategy_id,
                "candidate_id": trade.agent_consensus.get("candidate_id"),
                "entry_price": trade.entry_price,
                "rationale": trade.explanation,
            },
            decision_at=trade.entry_time,
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_REJECTED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        logger.info(
            "opportunity rejected",
            extra={"aitos_extra": {"symbol": opportunity.symbol, "reason": reason}},
        )
        return trade

    async def _trigger_exit(
        self, trade: Trade, price: float, reason: str, topic: str
    ) -> Trade:
        trade.state = TradeLifecycleState.EXIT_TRIGGERED
        trade.updated_at = utc_now_iso()
        try:
            await self._event_bus.publish(
                Event(
                    topic=topic,
                    payload={**trade.to_dict(), "trigger_price": price},
                    source_module=self.module_id,
                    priority=EventPriority.HIGH,
                )
            )
            return await self.close_trade(trade.trade_id, price, reason)
        except Exception:
            # If exit-event publication or closing fails, do not leave the
            # trade stuck in EXIT_TRIGGERED: the live price loop skips states
            # other than POSITION_OPENED, so this would disable further checks.
            if trade.trade_id in self._open_trades:
                trade.state = TradeLifecycleState.POSITION_OPENED
            raise

    def _assert_execution_unlatched(self, trade_id: str) -> None:
        if trade_id in self._execution_faults:
            raise ExecutionRecoveryRequired(
                "position",
                self._execution_faults[trade_id],
                "prior reduction/application requires reconciliation",
            )

    def _require_execution_writer(self) -> None:
        if not callable(getattr(self, "_execution_state_writer", None)):
            raise RuntimeError(
                "live execution requires synchronous durable state persistence"
            )
        if not callable(getattr(self, "_execution_position_verifier", None)):
            raise RuntimeError(
                "live execution requires authoritative position verification"
            )

    async def _execute_live_reduction(
        self, trade: Trade, quantity: float, price: float
    ) -> OrderResult:
        self._require_execution_writer()
        self._assert_execution_unlatched(trade.trade_id)
        if self._open_trades.get(trade.trade_id) is not trade:
            raise TradeNotFoundError(trade.trade_id)
        filled_ids = set(getattr(trade, "_aitos_filled_resting_order_ids", set()))
        if trade.trade_id in self._confirmed_reductions:
            client_id, result = self._confirmed_reductions[trade.trade_id]
            self._execution_faults[trade.trade_id] = client_id
        elif filled_ids:
            order_id = sorted(filled_ids)[0]
            self._execution_faults[trade.trade_id] = "resting:" + order_id
            evidence = await self._order_executor.get_order_status(
                trade.symbol, order_id
            )
            if not isinstance(evidence, dict) or evidence.get("status") not in {
                "FILLED",
                "CANCELED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
            }:
                raise ExecutionUncertainError(
                    trade.symbol, order_id, "resting fill is not confirmed"
                )
            result = OrderResult(
                order_id,
                trade.symbol,
                trade.side,
                float(evidence.get("executedQty", 0)),
                float(evidence.get("avgPrice", 0)),
            )
        else:
            identity = f"{trade.trade_id}|{float(trade.quantity).hex()}|{float(quantity).hex()}|{len(trade.partial_exits)}"
            client_id = (
                "aitos-exit-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            )
            self._execution_faults[trade.trade_id] = client_id
            result = await self._order_executor.submit_order(
                OrderRequest(
                    symbol=trade.symbol,
                    side=trade.side,
                    quantity=quantity,
                    reference_price=price,
                    reduce_only=True,
                    client_order_id=client_id,
                )
            )
        if not result.success:
            self._execution_faults.pop(trade.trade_id, None)
            raise RuntimeError(f"venue reduction rejected: {result.error}")
        if (
            not _valid_market_price(result.fill_price)
            or not _valid_market_price(result.filled_quantity)
            or result.filled_quantity > quantity * (1 + 1e-9)
        ):
            raise ExecutionUncertainError(
                trade.symbol, result.order_id, "invalid reduction evidence"
            )
        return result

    async def _refresh_live_protection(self, trade: Trade) -> None:
        # Keep the old protective stop until its replacement is accepted and
        # durably recorded. A failed replacement must not cancel protection.
        old_id = trade.sl_order_id
        stop_price = (
            self._emergency_stop_price(trade)
            if self._position_manager
            else trade.sl_price
        )
        result = await self._order_executor.place_stop_loss_order(
            trade.symbol, trade.side, trade.quantity, stop_price
        )
        if not result.success:
            raise ExecutionUncertainError(
                trade.symbol, trade.trade_id, "remaining exposure protection failed"
            )
        trade.sl_order_id = result.order_id
        if old_id and old_id != result.order_id:
            pending = trade.agent_consensus.setdefault(
                "_pending_stop_cancellations", []
            )
            if old_id not in pending:
                pending.append(old_id)
        trade.updated_at = utc_now_iso()
        try:
            await self._execution_state_writer(trade)
            for previous_id in list(
                trade.agent_consensus.get("_pending_stop_cancellations", [])
            ):
                await self._order_executor.cancel_resting_order(
                    trade.symbol, previous_id
                )
                trade.agent_consensus["_pending_stop_cancellations"].remove(previous_id)
            trade.updated_at = utc_now_iso()
            await self._execution_state_writer(trade)
            self._order_executor.acknowledge_protection(result, trade.trade_id)
        except BaseException:
            self._execution_recovery_required = True
            raise

    async def _commit_live_execution(self, trade: Trade) -> None:
        self._require_execution_writer()
        await self._execution_position_verifier()
        await self._execution_state_writer(trade)
        client_id = self._execution_faults.get(trade.trade_id)
        if client_id and not client_id.startswith("resting:"):
            self._order_executor.acknowledge_execution(client_id, trade.trade_id)
        self._execution_faults.pop(trade.trade_id, None)
        if hasattr(trade, "_aitos_filled_resting_order_ids"):
            del trade._aitos_filled_resting_order_ids

    async def _partial_close(
        self,
        trade: Trade,
        price: float,
        close_fraction: float,
        *,
        consume_tp: bool = False,
    ) -> None:
        if not self._order_executor.requires_exchange_execution:
            return await self._partial_close_unlocked(trade, price, close_fraction)
        if close_fraction == 1:
            await self.close_trade(trade.trade_id, price, "full_reduction_requested")
            return
        async with self._execution_locks.setdefault(trade.trade_id, asyncio.Lock()):
            self._assert_execution_unlatched(trade.trade_id)
            return await self._partial_close_unlocked(
                trade, price, close_fraction, consume_tp=consume_tp
            )

    async def _partial_close_unlocked(
        self,
        trade: Trade,
        price: float,
        close_fraction: float,
        *,
        execution_result: OrderResult | None = None,
        consume_tp: bool = False,
    ) -> None:
        if not _valid_market_price(price):
            raise ValueError(f"price must be a finite positive number, got {price!r}")
        if not 0.0 < close_fraction <= 1.0:
            raise ValueError(
                f"close_fraction must be in (0, 1], got {close_fraction!r}"
            )
        if trade.position_size_usd <= 0.0 or trade.quantity <= 0.0:
            raise ValueError(
                f"trade has no remaining position to close: {trade.trade_id}"
            )
        if self._order_executor.requires_exchange_execution:
            execution_result = execution_result or await self._execute_live_reduction(
                trade, trade.quantity * close_fraction, price
            )
            price = execution_result.fill_price
            close_fraction = execution_result.filled_quantity / trade.quantity
        direction = 1 if trade.side == TradeSide.LONG else -1
        pnl_pct = ((price - trade.entry_price) / trade.entry_price) * direction
        # Keep full precision internally. Rounding the USD ledger to cents while
        # reducing quantity at full precision causes quantity/notional drift,
        # especially when the remaining position is only a few cents.
        closed_notional = trade.position_size_usd * close_fraction
        closed_quantity = trade.quantity * close_fraction
        remaining_notional = trade.position_size_usd - closed_notional
        remaining_quantity = trade.quantity - closed_quantity
        realized = closed_notional * pnl_pct
        trade.partial_exits.append(
            PartialExit(
                price=price,
                size_usd=closed_notional,
                r_multiple=round(trade.unrealized_r_multiple(price), 4),
            )
        )
        trade.position_size_usd = max(0.0, remaining_notional)
        trade.quantity = max(0.0, remaining_quantity)
        if self._order_executor.requires_exchange_execution:
            trade.risk_amount_usd *= 1 - close_fraction
        trade.gross_realized_pnl_usd += realized
        trade.pnl = round(
            trade.gross_realized_pnl_usd - trade.fees_paid_usd + trade.funding_pnl_usd,
            8,
        )
        trade.updated_at = utc_now_iso()
        if self._order_executor.requires_exchange_execution:
            consumed = set(getattr(trade, "_aitos_filled_resting_order_ids", set()))
            if consume_tp:
                if trade.tp_order_ids:
                    consumed_id = trade.tp_order_ids.pop(0)
                    await self._order_executor.cancel_resting_order(
                        trade.symbol, consumed_id
                    )
                if trade.take_profit_levels:
                    trade.take_profit_levels.pop(0)
            consumed_tp = consumed.intersection(trade.tp_order_ids)
            trade.tp_order_ids = [
                order_id for order_id in trade.tp_order_ids if order_id not in consumed
            ]
            if consumed_tp and trade.take_profit_levels:
                trade.take_profit_levels.pop(0)
            if self._use_exchange_side_stops and trade.quantity > 0:
                await self._refresh_live_protection(trade)
            await self._commit_live_execution(trade)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_PARTIAL_CLOSE,
                payload={
                    **trade.to_dict(),
                    "closed_notional_usd": closed_notional,
                    "closed_quantity": closed_quantity,
                    "realized_pnl": realized,
                },
                source_module=self.module_id,
            )
        )

    def configure_position_switching(self, coordinator: Any) -> None:
        """Attach the durable, fail-closed position-switch coordinator.

        Switching remains unavailable until the caller supplies a persistent
        journal, an idempotent reduce-only close adapter and venue-flat check.
        """
        if coordinator is None:
            raise ValueError("position switch coordinator is required")
        if getattr(coordinator, "lifecycle", None) is not self:
            raise ValueError("coordinator must be bound to this TradeLifecycle")
        self._position_switch_coordinator = coordinator
        self._switch_recovery_report = None
        # The switching coordinator and the final submit guard must share one
        # reservation ledger so incumbent release and candidate authorization
        # reconcile against the same source of truth.
        self._aitos_capital_gateway = coordinator.gateway

    async def evaluate_rebalance_scan_cycle(
        self, opportunities: list[Opportunity]
    ) -> list[tuple[Trade, Opportunity, Any]]:
        """Rank all scanner-approved opportunities against current open positions."""
        coordinator = self._position_switch_coordinator
        if coordinator is None:
            return []
        return await coordinator.evaluate_scan_cycle(opportunities)

    async def switch_position(
        self, incumbent: Trade, candidate: Opportunity, decision: Any
    ) -> Trade | None:
        """Execute one approved replacement through the durable switch state machine."""
        self._require_initialized()
        coordinator = self._position_switch_coordinator
        if coordinator is None:
            raise RuntimeError("safe position switching is not configured")
        return await coordinator.switch(
            incumbent=incumbent, candidate=candidate, decision=decision
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "TradeLifecycle.initialize() must be called first"
            )
