"""OpportunityScanner — multi-source market intelligence."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite, log
from typing import Any

from aitos.config.paths import get_model_data_dir
from aitos.config.tuning import TuningSettings, load_tuning_settings
from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence import indicators
from aitos.intelligence.a_stat_bridge import AStatBridge
from aitos.statistics.decision_ledger import DecisionOutcomeLedger
from aitos.statistics.financial_ml_pipeline import FinancialMLPipeline
from aitos.intelligence.amt.engine import AMTContext, AMTEngine
from aitos.intelligence.auction import auction_context_score
from aitos.intelligence.candidate_rotation import CandidateRotationManager
from aitos.intelligence.entry_quality import (
    breakout_acceptance_score,
    relative_strength_score,
)
from aitos.intelligence.footprint import FootprintEngine
from aitos.intelligence.footprint_signals import FootprintSignalEngine
from aitos.intelligence.funding import funding_rate_score
from aitos.intelligence.htf_cache import HigherTimeframeCandleCache
from aitos.intelligence.liquidity import (
    absorption_proxy_score,
    adverse_flow_without_progress,
    depth_event_flow_analysis,
    depth_imbalance_score,
    liquidity_intelligence_score,
    liquidity_quality_score,
    liquidity_wall_score,
    sweep_potential_score,
)
from aitos.intelligence.liquidity_tracker import LiquidityTracker
from aitos.intelligence.live_auction import live_auction_score
from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.intelligence.open_interest import oi_trend_score
from aitos.intelligence.opportunity_outcomes import (
    CandidateOutcomeStore,
    DirectionalFunnelStore,
    RankingHysteresis,
)
from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.intelligence.orderflow_liquidity_interaction import (
    FlowLiquidityInteractionEngine,
)
from aitos.intelligence.rl_policy import NeutralRLScorer, RLPolicyScorer
from aitos.logging_setup import get_logger
from aitos.models.trade import Opportunity, TradeSide

logger = get_logger("aitos.intelligence.scanner")
_ACTIVE_SCAN_TRACE_ID: ContextVar[str] = ContextVar("aitos_scan_trace_id", default="")
TOPIC_SCAN_COMPLETE = "market.opportunity_scanned"
DEFAULT_WEIGHTS: dict[str, float] = {
    "trend_strength": 0.10,
    "liquidity_quality": 0.10,
    "order_flow_bias": 0.15,
    "auction_context": 0.10,
    "volatility": 0.05,
    "market_regime": 0.10,
    "lead_lag": 0.10,
    "funding_rate": 0.08,
    "open_interest_trend": 0.08,
    "rl_confidence": 0.04,
    "footprint_interaction": 0.10,
    "a_stat": 0.08,
    "order_book_flow_imbalance": 0.08,
    "entry_timing": 0.07,
    "breakout_acceptance": 0.07,
    "relative_strength": 0.08,
    "htf_trend_alignment": 0.10,
}
REGIME_FIT_SCORE = {
    "trending": 9.0,
    "ranging": 4.0,
    "volatile": 3.0,
    "compression": 5.5,
    "expansion": 6.5,
    "unknown": 5.0,
}

# The HTF is deliberately slower than the entry timeframe. Explicit overrides
# remain available through ScannerTuning for instruments with different cadence.
HTF_TIMEFRAME_MAP = {
    "1m": "5m",
    "3m": "15m",
    "5m": "15m",
    "15m": "1h",
    "30m": "2h",
    "1h": "4h",
    "2h": "1d",
    "4h": "1d",
    "6h": "1d",
    "8h": "1d",
    "12h": "1d",
    "1d": "1w",
    "1w": "1M",
}


def resolve_higher_timeframe(entry_timeframe: str, configured: str = "auto") -> str:
    """Resolve an explicitly configured HTF or the conservative default mapping."""
    if configured != "auto":
        return configured
    try:
        return HTF_TIMEFRAME_MAP[entry_timeframe]
    except KeyError as exc:
        raise ValueError(
            f"No automatic higher timeframe mapping for {entry_timeframe!r}; "
            "configure scanner.htf_timeframe explicitly"
        ) from exc


def higher_timeframe_alignment(
    klines: list,
    direction: TradeSide,
    *,
    min_adx: float = 25.0,
    min_di_gap: float = 5.0,
) -> tuple[bool, dict[str, Any]]:
    """Require a directional, non-choppy HTF trend aligned with the LTF entry.

    Uses only completed HTF bars. ADX measures trend strength while +DI/-DI
    supplies direction; market structure is an additional conflict veto.
    Insufficient/neutral data fails closed rather than treating missing data
    as confirmation.
    """
    diagnostics: dict[str, Any] = {
        "trend": "unknown",
        "adx": 0.0,
        "plus_di": 0.0,
        "minus_di": 0.0,
        "di_gap": 0.0,
        "structure_bias": "neutral",
        "reason": "insufficient_htf_history",
    }
    if len(klines) < 30:
        return False, diagnostics

    trend_adx = indicators.adx(klines)
    plus_di, minus_di = indicators.di_plus_minus(klines)
    di_gap = abs(plus_di - minus_di)
    structure = indicators.analyze_market_structure(klines)
    trend = "neutral"
    if trend_adx >= min_adx and di_gap >= min_di_gap:
        trend = "bullish" if plus_di > minus_di else "bearish"

    diagnostics.update(
        {
            "trend": trend,
            "adx": trend_adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "di_gap": di_gap,
            "structure_bias": structure.bias,
            "reason": "htf_trend_aligned",
        }
    )
    if trend == "neutral":
        diagnostics["reason"] = "htf_trend_weak_or_choppy"
        return False, diagnostics
    if structure.bias in {"bullish", "bearish"} and structure.bias != trend:
        diagnostics["reason"] = "htf_structure_conflicts_with_directional_movement"
        return False, diagnostics

    aligned = (trend == "bullish" and direction == TradeSide.LONG) or (
        trend == "bearish" and direction == TradeSide.SHORT
    )
    if not aligned:
        diagnostics["reason"] = "ltf_signal_opposes_htf_trend"
    return aligned, diagnostics


def entry_liquidity_depth_rejection(
    *,
    direction: TradeSide,
    liquidity_quality: float,
    depth_analysis: dict[str, Any],
    min_liquidity_quality: float = 5.0,
    max_directional_cancel_pressure: float = 0.6,
) -> tuple[str, dict[str, Any]] | None:
    """Return a hard entry veto for weak execution quality or fresh side pressure.

    Missing/stale depth evidence is not interpreted as spoofing. The liquidity
    floor is independent and always enforced; cancellation pressure is only
    applied when the depth-event analyzer marks the sample fresh and reliable.
    """
    try:
        quality = float(liquidity_quality)
    except (TypeError, ValueError):
        quality = float("nan")
    if not isfinite(quality) or quality < min_liquidity_quality:
        return (
            "liquidity_quality_below_floor",
            {
                "liquidity_quality": quality if isfinite(quality) else None,
                "min_liquidity_quality": min_liquidity_quality,
            },
        )

    if not bool(depth_analysis.get("ofi_available")):
        return None

    pressure_key = (
        "bid_inferred_cancel_pressure"
        if direction == TradeSide.LONG
        else "ask_inferred_cancel_pressure"
    )
    try:
        pressure = float(depth_analysis.get(pressure_key))
    except (TypeError, ValueError):
        pressure = float("nan")
    if not isfinite(pressure):
        return (
            "directional_cancel_pressure_unavailable",
            {
                "direction": direction.value,
                "required_depth_metric": pressure_key,
                "ofi_confidence": depth_analysis.get("ofi_confidence", 0.0),
                "depth_freshness_ms": depth_analysis.get("freshness_ms", -1.0),
            },
        )
    if pressure > max_directional_cancel_pressure:
        return (
            "directional_cancel_pressure_too_high",
            {
                "direction": direction.value,
                "cancel_pressure_side": "bid" if direction == TradeSide.LONG else "ask",
                "directional_cancel_pressure": pressure,
                "max_directional_cancel_pressure": max_directional_cancel_pressure,
                "ofi_confidence": depth_analysis.get("ofi_confidence", 0.0),
                "depth_freshness_ms": depth_analysis.get("freshness_ms", -1.0),
            },
        )
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_iso(value: Any, fallback: str) -> str:
    """Normalize exchange candle timestamps to timezone-aware ISO-8601."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and isfinite(float(value)):
            timestamp = float(value)
            if abs(timestamp) > 100_000_000_000:
                timestamp /= 1000.0
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    direction: TradeSide
    composite_score: float
    component_scores: dict[str, float]
    component_availability: dict[str, bool]
    rationale: list[str]
    entry_price: float
    atr: float
    regime: str
    scanned_at: str = field(default_factory=_utc_now_iso)
    htf_adx: float = 0.0
    trace_id: str | None = None
    statistical_forecast: dict[str, Any] | None = None
    candidate_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    financial_ml_features: dict[str, float] = field(default_factory=dict)
    meta_raw_probability: float | None = None
    meta_probability: float | None = None
    meta_accepted: bool | None = None
    meta_reason: str | None = None


def _volatility_fitness(
    atr_percentile: float, sweet_spot: float = 60.0, tolerance: float = 6.0
) -> float:
    return round(
        max(0.0, min(10.0, 10.0 - abs(atr_percentile - sweet_spot) / tolerance)), 2
    )


def determine_direction(
    structure_direction: str,
    cvd_score: float,
    *,
    structure_bias: str | None = None,
    structure_event: str | None = None,
) -> TradeSide | None:
    """Map structure + CVD into a trade side.

    Optional ``structure_bias`` / ``structure_event`` refine the decision:
    - CVD-only (structure_direction=="none") is blocked when it fights bias.
    - CHOCH may flip with confirming flow even against prior bias.
    """
    bias = (structure_bias or "").lower() or None
    event = (structure_event or "").lower() or None

    # CHOCH: allow structural flip when flow confirms
    if event == "choch":
        if structure_direction == "bullish_bos" and cvd_score >= 6.5:
            return TradeSide.LONG
        if structure_direction == "bearish_bos" and cvd_score <= 3.5:
            return TradeSide.SHORT
        return None

    if structure_direction == "bullish_bos" and cvd_score >= 6.5:
        return TradeSide.LONG
    if structure_direction == "bearish_bos" and cvd_score <= 3.5:
        return TradeSide.SHORT
    if structure_direction == "none":
        if cvd_score >= 6.5:
            if bias == "bearish":
                return None
            return TradeSide.LONG
        if cvd_score <= 3.5:
            if bias == "bullish":
                return None
            return TradeSide.SHORT
    return None


class OpportunityScanner(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        exchange: ExchangeAdapter,
        symbols: list[str],
        timeframe: str = "15m",
        reference_symbol: str = "BTCUSDT",
        rl_scorer: RLPolicyScorer | None = None,
        weights: dict[str, float] | None = None,
        min_score_threshold: float = 60.0,
        top_n: int | None = None,
        kline_lookback: int = 100,
        trade_lookback: int = 500,
        footprint_tick_sizes: dict[str, float] | None = None,
        live_state_stale_seconds: float = 5.0,
        amt_value_area_pct: float = 0.70,
        amt_ib_minutes: int = 60,
        tuning: TuningSettings | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._exchange = exchange
        self._tuning = tuning or load_tuning_settings()
        scanner_tuning = self._tuning.scanner
        configured_symbols = list(symbols)
        if scanner_tuning.universe_mode == "allowlist":
            allowed = set(scanner_tuning.symbol_allowlist)
            configured_symbols = [
                symbol for symbol in configured_symbols if symbol.upper() in allowed
            ]
        self._symbols = configured_symbols[: scanner_tuning.universe_limit]
        self._timeframe = timeframe
        self._reference_symbol = reference_symbol
        self._rl_scorer = rl_scorer or NeutralRLScorer()
        self._weights = weights or DEFAULT_WEIGHTS
        self._min_score_threshold = min_score_threshold
        self._top_n = top_n if top_n is not None else scanner_tuning.top_n
        self._scan_semaphore = asyncio.Semaphore(
            min(
                self._tuning.analysis.expensive_analysis_concurrency,
                self._tuning.hardware.cpu_worker_budget,
            )
        )
        self._kline_lookback, self._trade_lookback = kline_lookback, trade_lookback
        self._footprint_tick_sizes = footprint_tick_sizes or {}
        self._live_state_stale_seconds = max(0.5, live_state_stale_seconds)
        self._amt_value_area_pct = amt_value_area_pct
        self._amt_ib_minutes = amt_ib_minutes
        self._initialized = False
        self._last_oi = {}
        self._htf_cache = HigherTimeframeCandleCache(
            max_requests=self._tuning.scanner.htf_request_budget_per_minute,
            window_seconds=60.0,
            max_concurrent_fetches=self._tuning.scanner.htf_fetch_concurrency,
        )
        self._last_order_books = {}
        self._last_order_book_sources = {}
        self._last_amt_profile = {}
        self._last_amt_context = {}
        self._liquidity_trackers = {}
        self._footprint_engines = {}
        self._amt_engines = {}
        self._footprint_signal_engine = FootprintSignalEngine()
        self._interaction_engine = FlowLiquidityInteractionEngine()
        self._a_stat_bridge = AStatBridge()
        self._live_cache = LiveScannerCache(
            event_bus,
            self._symbols,
            max_trades=max(5000, trade_lookback),
            hawkes_engine=self._a_stat_bridge.engine,
        )
        self._last_scan_at = None
        self._last_candidate_count = 0
        self._candidate_rotation = CandidateRotationManager(
            default_cooldown_seconds=self._tuning.analysis.rejection_cooldown_seconds,
            kernel_cooldown_seconds=self._tuning.analysis.kernel_rejection_cooldown_seconds,
            market_change_recheck_pct=self._tuning.analysis.market_change_recheck_pct,
        )
        self._lightweight_fingerprints: dict[str, str] = {}
        self._candidate_outcomes = CandidateOutcomeStore(
            get_model_data_dir() / "candidate_outcomes.sqlite3"
        )
        self._directional_funnel = DirectionalFunnelStore(
            get_model_data_dir() / "directional_funnel.sqlite3"
        )
        self._decision_outcome_ledger = DecisionOutcomeLedger(
            get_model_data_dir() / "decision_outcomes.sqlite3"
        )
        self._financial_ml = FinancialMLPipeline(
            get_model_data_dir() / "financial_ml.sqlite3"
        )
        self._ranking_hysteresis = RankingHysteresis(
            margin_points=self._tuning.analysis.ranking_hysteresis_points
        )

    def apply_tuning_settings(self, settings: TuningSettings) -> None:
        """Apply runtime-safe scanner budgets; universe changes require restart."""
        previous = self._tuning.scanner
        incoming = settings.scanner
        if (
            previous.universe_mode != incoming.universe_mode
            or previous.universe_limit != incoming.universe_limit
            or previous.symbol_allowlist != incoming.symbol_allowlist
        ):
            raise ValueError(
                "Runtime reload changed scanner universe settings; restart AITOS "
                "to rebuild market-data subscriptions safely"
            )
        self._tuning = settings
        self._top_n = incoming.top_n
        self._ranking_hysteresis.margin_points = (
            settings.analysis.ranking_hysteresis_points
        )
        self._scan_semaphore = asyncio.Semaphore(
            min(
                settings.analysis.expensive_analysis_concurrency,
                settings.hardware.cpu_worker_budget,
            )
        )

    @property
    def module_id(self) -> str:
        return "opportunity-scanner"

    @property
    def version(self) -> str:
        return "1.10.1"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        await self._exchange.connect()
        await self._live_cache.initialize(direct_market_data=True)
        missing = [s for s in self._symbols if s not in self._footprint_tick_sizes]
        if missing:
            try:
                filters = await self._exchange.fetch_exchange_info(missing)
                for symbol, symbol_filter in filters.items():
                    if symbol_filter.tick_size > 0:
                        self._footprint_tick_sizes[symbol] = symbol_filter.tick_size
                logger.info(
                    "loaded footprint tick sizes",
                    extra={
                        "aitos_extra": {
                            "symbols": list(filters),
                            "missing": [
                                s
                                for s in missing
                                if s not in self._footprint_tick_sizes
                            ],
                        }
                    },
                )
            except Exception as exc:
                logger.warning(
                    "could not auto-load footprint tick sizes",
                    extra={"aitos_extra": {"error": str(exc), "symbols": missing}},
                )
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_scan_at,
            details={
                "last_candidate_count": self._last_candidate_count,
                "symbols_tracked": len(self._symbols),
                "footprint_tick_sizes_loaded": len(self._footprint_tick_sizes),
                "amt_symbols": len(self._amt_engines),
                "live_state_symbols": sum(
                    1 for s in self._symbols if self._live_cache.snapshot(s) is not None
                ),
                "live_state_symbols_by_symbol": [
                    symbol
                    for symbol in self._symbols
                    if self._live_cache.snapshot(symbol) is not None
                ],
                "live_trade_events_by_symbol": {
                    symbol: len(state.trades)
                    for symbol in self._symbols
                    if (state := self._live_cache.snapshot(symbol)) is not None
                    and state.trades
                },
                "hawkes_models": self._a_stat_bridge.engine.hawkes_model_status(),
                "decision_outcome_ledger": self._decision_outcome_ledger.snapshot(),
                "live_hawkes": {
                    symbol: {
                        "last_event_timestamp": state.last_hawkes_timestamp,
                        "intensities": state.last_hawkes_intensities,
                        "event_rejections": state.hawkes_event_rejections,
                        "fit_status": state.hawkes_fit_status,
                        "fit_attempts": state.hawkes_fit_attempts,
                        "fit_events": state.hawkes_fit_events,
                        "fit_error": state.hawkes_fit_error,
                    }
                    for symbol in self._symbols
                    if (state := self._live_cache.snapshot(symbol)) is not None
                    and (
                        state.last_hawkes_timestamp is not None
                        or state.hawkes_fit_attempts > 0
                    )
                },
                "candidate_rotation": self._candidate_rotation.snapshot(),
                "candidate_outcomes": self._candidate_outcomes.snapshot(),
                "financial_ml": self._financial_ml.snapshot(),
                "directional_funnel": self._directional_funnel.snapshot(),
                "ranking_hysteresis": self._ranking_hysteresis.snapshot(),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        await self._live_cache.shutdown()
        while self._reconcile_financial_ml_labels():
            pass
        self._candidate_outcomes.close()
        self._directional_funnel.close()
        self._financial_ml.close()
        await self._exchange.close()

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def accept_live_trade(self, trade):
        state = self._live_cache.snapshot(trade.symbol)
        previous_trade_id = (
            state.trades[-1].trade_id if state is not None and state.trades else None
        )
        await self._live_cache.accept_live_trade(trade)
        updated = self._live_cache.snapshot(trade.symbol)
        accepted = (
            updated is not None
            and bool(updated.trades)
            and updated.trades[-1].trade_id == trade.trade_id
            and trade.trade_id != previous_trade_id
        )
        if accepted:
            timestamp = getattr(trade, "timestamp", None)
            quantity = float(getattr(trade, "quantity", 0.0) or 0.0)
            trade_side = str(
                getattr(
                    getattr(trade, "side", None), "value", getattr(trade, "side", "BUY")
                )
            ).upper()
            signed_volume = quantity if trade_side == "BUY" else -quantity
            try:
                from aitos.statistics.financial_ml import MarketEvent

                event = MarketEvent(
                    timestamp=_timestamp_iso(timestamp, _utc_now_iso()),
                    price=float(trade.price),
                    volume=quantity,
                    signed_volume=signed_volume,
                    event_id=f"{trade.symbol.upper()}:{trade.trade_id}",
                )
                await asyncio.to_thread(
                    self._financial_ml.ingest_market_event, trade.symbol, event
                )
                await asyncio.to_thread(
                    self._financial_ml.observe_price,
                    trade.symbol,
                    float(trade.price),
                    timestamp,
                )
                await asyncio.to_thread(self._reconcile_financial_ml_labels)
            except Exception:
                logger.exception(
                    "financial ML live event ingestion failed",
                    extra={"aitos_extra": {"symbol": trade.symbol}},
                )
            self._candidate_outcomes.update_price(trade.symbol, trade.price, timestamp)
            await asyncio.to_thread(self._reconcile_candidate_markouts)

    async def accept_live_order_book(self, book):
        await self._live_cache.accept_live_order_book(book)

    async def _record_funnel(self, **event: Any) -> None:
        """Persist diagnostics off-loop; telemetry must not change trade decisions."""
        trace_id = _ACTIVE_SCAN_TRACE_ID.get()
        if trace_id:
            details = dict(event.get("details") or {})
            details.setdefault("trace_id", trace_id)
            event["details"] = details
        try:
            await asyncio.to_thread(self._directional_funnel.record, **event)
        except Exception:
            logger.exception(
                "directional funnel persistence failed",
                extra={
                    "aitos_extra": {
                        "symbol": event.get("symbol"),
                        "stage": event.get("stage"),
                    }
                },
            )
        if event.get("symbol") and event.get("stage"):
            details = dict(event.get("details") or {})
            candidate_id = details.get("candidate_id")
            attribution_id = str(
                candidate_id or trace_id or f"scan:{event['symbol']}:{time.time_ns()}"
            )
            try:
                await asyncio.to_thread(
                    self._decision_outcome_ledger.record_decision,
                    (
                        f"{attribution_id}:{event.get('symbol')}:"
                        f"{event.get('side', 'UNKNOWN')}:{event['stage']}:{time.time_ns()}"
                    ),
                    entity_id=attribution_id,
                    symbol=str(event["symbol"]),
                    side=str(event.get("side") or "UNKNOWN"),
                    decision_type="GATE",
                    accepted=bool(event.get("accepted", False)),
                    reason=str(event.get("reason") or event["stage"]),
                    payload={
                        "stage": str(event["stage"]),
                        "details": details,
                        "candidate_id": candidate_id,
                    },
                    candidate_id=str(candidate_id) if candidate_id else None,
                )
            except Exception:
                logger.exception(
                    "scanner decision attribution persistence failed",
                    extra={
                        "aitos_extra": {
                            "symbol": event.get("symbol"),
                            "stage": event.get("stage"),
                            "trace_id": attribution_id,
                        }
                    },
                )

    def _footprint_tick_size(self, symbol: str) -> float | None:
        tick = self._footprint_tick_sizes.get(symbol)
        return tick if tick is not None and tick > 0 else None

    def _live_market_data(self, symbol: str) -> tuple[list, Any, bool]:
        state = self._live_cache.snapshot(symbol)
        if state is None or state.last_trade_at is None or state.last_book_at is None:
            return [], None, False
        now = datetime.now(timezone.utc)
        return (
            list(state.trades),
            state.order_book,
            (now - state.last_trade_at).total_seconds()
            <= self._live_state_stale_seconds
            and (now - state.last_book_at).total_seconds()
            <= self._live_state_stale_seconds,
        )

    def _analyze_amt(
        self, symbol: str, trades: list, klines: list, order_book: Any
    ) -> AMTContext | None:
        tick_size = self._footprint_tick_size(symbol)
        if tick_size is None or not trades:
            return None
        engine = self._amt_engines.setdefault(
            symbol, AMTEngine(tick_size, self._amt_value_area_pct, self._amt_ib_minutes)
        )
        context = engine.analyze(
            trades,
            klines=klines,
            book=order_book,
            previous_profile=self._last_amt_profile.get(symbol),
        )
        self._last_amt_profile[symbol] = context.profile
        self._last_amt_context[symbol] = context
        return context

    async def scan_symbol(
        self, symbol: str, reference_klines: list | None = None
    ) -> ScanCandidate | None:
        """Measure scanner stage execution without changing signal decisions."""
        import time

        started = time.perf_counter()
        trace_token = None
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_error,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.SCANNER.value, symbol=symbol)
            state = self._live_cache.snapshot(symbol)
            trace_id = (
                (state.last_trade_trace_id or state.last_book_trace_id)
                if state is not None
                else None
            )
            trace_event_id = (
                (state.last_trade_event_id or state.last_book_event_id)
                if state is not None
                else None
            )
            if not trace_id:
                trace_id = f"scan:{symbol.upper()}:{time.time_ns()}"
            trace_token = _ACTIVE_SCAN_TRACE_ID.set(trace_id)
            note_trace(
                trace_id=trace_id,
                stage=PipelineStage.SCANNER.value,
                symbol=symbol,
                event_id=trace_event_id,
                extra={"phase": "scan_started"},
            )
        except Exception:
            pass
        try:
            result = await self._scan_symbol_impl(symbol, reference_klines)
        except Exception as exc:
            try:
                record_error(
                    PipelineStage.SCANNER.value,
                    symbol=symbol,
                    issue="processing_error",
                )
            except Exception:
                pass
            try:
                await self._record_funnel(
                    symbol=symbol,
                    side="UNKNOWN",
                    stage="scanner_error",
                    accepted=False,
                    reason=type(exc).__name__,
                    details={"error": str(exc)[:500]},
                )
            except Exception:
                pass
            raise
        finally:
            if trace_token is not None:
                _ACTIVE_SCAN_TRACE_ID.reset(trace_token)
        try:
            latency_ms = (time.perf_counter() - started) * 1000.0
            record_output(
                PipelineStage.SCANNER.value,
                symbol=symbol,
                latency_ms=latency_ms,
            )
            note_trace(
                trace_id=trace_id,
                stage=PipelineStage.SCANNER.value,
                symbol=symbol,
                latency_ms=latency_ms,
                event_id=trace_event_id,
                extra={
                    "phase": "scan_completed",
                    "candidate_found": result is not None,
                },
            )
        except Exception:
            pass
        return result

    async def _scan_symbol_impl(
        self, symbol: str, reference_klines: list | None = None
    ) -> ScanCandidate | None:
        self._require_initialized()
        klines = await self._exchange.fetch_klines(
            symbol, self._timeframe, limit=self._kline_lookback
        )
        # REST may return the currently-forming candle. Structural signals
        # must be evaluated only on completed bars, irrespective of source.
        klines = [k for k in klines if getattr(k, "is_closed", True)]
        if len(klines) < 20:
            await self._record_funnel(
                symbol=symbol,
                side="LONG",
                stage="market_data_gate",
                accepted=False,
                reason="insufficient_klines",
                details={"kline_count": len(klines), "minimum": 20},
            )
            await self._record_funnel(
                symbol=symbol,
                side="SHORT",
                stage="market_data_gate",
                accepted=False,
                reason="insufficient_klines",
                details={"kline_count": len(klines), "minimum": 20},
            )
            logger.info(
                "paper signal diagnostics",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "reason": "insufficient_klines",
                        "kline_count": len(klines),
                    }
                },
            )
            return None
        live_trades, live_book, live_fresh = self._live_market_data(symbol)
        if self._tuning.mode == "live" and not live_fresh:
            for rejected_side in ("LONG", "SHORT"):
                await self._record_funnel(
                    symbol=symbol,
                    side=rejected_side,
                    stage="market_data_gate",
                    accepted=False,
                    reason="live_market_data_stale_or_unavailable",
                )
            logger.warning(
                "entry blocked: live market data is stale or unavailable",
                extra={
                    "aitos_extra": {"symbol": symbol, "market_source": "rest_fallback"}
                },
            )
            return None
        if live_fresh and live_trades and live_book is not None:
            trades, order_book, market_source = (
                live_trades[-self._trade_lookback :],
                live_book,
                "websocket_live_state",
            )
        else:
            order_book, trades, market_source = (
                await self._exchange.fetch_order_book(symbol, limit=20),
                await self._exchange.fetch_recent_trades(
                    symbol, limit=self._trade_lookback
                ),
                "rest_fallback",
            )
        funding = await self._exchange.fetch_funding_rate(symbol)
        oi_current = await self._exchange.fetch_open_interest(symbol)
        oi_previous = self._last_oi.get(symbol)
        atr = indicators.average_true_range(klines)
        vol_percentile = indicators.atr_percentile(klines)
        regime = indicators.classify_regime(klines)
        market_structure = indicators.analyze_market_structure(klines)
        structure_direction, structure_strength = (
            market_structure.bos_direction,
            market_structure.bos_strength,
        )
        flow_features = (
            OrderFlowEngine(max_trades=max(100, self._trade_lookback)).ingest_many(
                trades
            )
            if trades
            else None
        )
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            state = self._live_cache.snapshot(symbol)
            trace_id = (
                (state.last_trade_trace_id or state.last_book_trace_id)
                if state is not None
                else None
            ) or f"feature:{symbol.upper()}:{time.time_ns()}"
            trace_event_id = (
                (state.last_trade_event_id or state.last_book_event_id)
                if state is not None
                else None
            )
            record_input(PipelineStage.FEATURE.value, symbol=symbol)
            record_output(PipelineStage.FEATURE.value, symbol=symbol)
            note_trace(
                trace_id=trace_id,
                stage=PipelineStage.FEATURE.value,
                symbol=symbol,
                event_id=trace_event_id,
                extra={
                    "phase": "features_ready",
                    "market_source": market_source,
                },
            )
        except Exception:
            pass
        candle_cvd = indicators.cvd_trend_score(klines)
        flow_score = flow_features.bias_score if flow_features else candle_cvd
        direction = determine_direction(
            structure_direction,
            flow_score,
            structure_bias=market_structure.bias,
            structure_event=market_structure.event,
        )
        from aitos.intelligence.adaptive_mtf import directional_diagnostics

        directional_funnel = directional_diagnostics(
            structure_direction,
            flow_score,
            structure_bias=market_structure.bias,
            structure_event=market_structure.event,
        )
        selected_side = (
            directional_funnel["long"]
            if direction == TradeSide.LONG
            else directional_funnel["short"] if direction == TradeSide.SHORT else None
        )
        candidate_rejection_reasons = (
            list(selected_side["rejection_reasons"])
            if selected_side is not None
            else sorted(
                set(
                    directional_funnel["long"]["rejection_reasons"]
                    + directional_funnel["short"]["rejection_reasons"]
                )
            )
        )
        try:
            for side_key in ("long", "short"):
                side_diagnostic = directional_funnel[side_key]
                await self._record_funnel(
                    symbol=symbol,
                    side=side_key,
                    stage="directional_gate",
                    accepted=side_diagnostic["accepted_by_diagnostic"],
                    reason=(
                        "accepted"
                        if side_diagnostic["accepted_by_diagnostic"]
                        else ",".join(side_diagnostic["rejection_reasons"])
                    ),
                    details={
                        "structure_direction": structure_direction,
                        "structure_bias": market_structure.bias,
                        "structure_event": market_structure.event,
                        "cvd_score": round(flow_score, 4),
                    },
                )
            await self._record_funnel(
                symbol=symbol,
                side=direction.value if direction else "UNKNOWN",
                stage="direction_selection",
                accepted=direction is not None,
                reason="selected" if direction is not None else "no_directional_setup",
                details={"cvd_score": round(flow_score, 4)},
            )
        except Exception:
            logger.exception(
                "could not persist directional funnel event",
                extra={"aitos_extra": {"symbol": symbol}},
            )
        logger.info(
            "directional signal funnel",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "structure_direction": structure_direction,
                    "structure_bias": market_structure.bias,
                    "structure_event": market_structure.event,
                    "cvd_score": round(flow_score, 4),
                    "selected_direction": direction.value if direction else "none",
                    "selected_rejection_reasons": candidate_rejection_reasons,
                    "long": directional_funnel["long"],
                    "short": directional_funnel["short"],
                }
            },
        )
        # Keep the OI baseline fresh even when the HTF gate rejects this entry.
        self._last_oi[symbol] = oi_current
        htf_alignment_score = 5.0
        htf_available = False
        htf_diagnostics: dict[str, Any] = {
            "trend": "disabled",
            "reason": "htf_filter_disabled",
        }
        if direction is not None and self._tuning.scanner.htf_filter_enabled:
            try:
                from aitos.intelligence.multi_timeframe import MultiTimeframeAnalyzer

                mtf = MultiTimeframeAnalyzer(
                    self._htf_cache,
                    lookback=self._tuning.scanner.htf_lookback,
                    freshness_grace_seconds=self._tuning.scanner.htf_freshness_grace_seconds,
                )
                mtf_result = await mtf.analyze(
                    self._exchange,
                    symbol,
                    direction,
                    klines,
                    flow_score=flow_score,
                    structure_bias=market_structure.bias,
                    allow_neutral=self._tuning.scanner.htf_allow_neutral,
                    neutral_min_score=self._tuning.scanner.htf_neutral_min_ltf_score,
                    countertrend_min_score=self._tuning.scanner.htf_countertrend_min_ltf_score,
                    alignment_balance_threshold=self._tuning.scanner.htf_alignment_balance_threshold,
                )
                htf_diagnostics = dict(mtf_result.diagnostics)
                htf_available = bool(mtf_result.diagnostics["available_timeframes"])
                await self._record_funnel(
                    symbol=symbol,
                    side=direction.value,
                    stage="mtf_gate",
                    accepted=mtf_result.accepted,
                    reason=mtf_result.decision.reason,
                    details=htf_diagnostics,
                )
                if not mtf_result.accepted:
                    await self._record_funnel(
                        symbol=symbol,
                        side=direction.value,
                        stage="scanner_terminal",
                        accepted=False,
                        reason="mtf_rejected",
                        details={"mtf_reason": mtf_result.decision.reason},
                    )
                    logger.info(
                        "entry rejected by six-timeframe adaptive policy",
                        extra={
                            "aitos_extra": {
                                "symbol": symbol,
                                "direction": direction.value,
                                "entry_timeframe": self._timeframe,
                                "mtf": htf_diagnostics,
                            }
                        },
                    )
                    return None
                mode = mtf_result.decision.mode
                htf_alignment_score = (
                    10.0
                    if mode == "aligned"
                    else 7.0 if mode == "neutral_or_mixed" else 6.0
                )
            except Exception as exc:
                await self._record_funnel(
                    symbol=symbol,
                    side=direction.value,
                    stage="mtf_gate",
                    accepted=False,
                    reason="mtf_analysis_error",
                    details={"error": str(exc)[:500]},
                )
                await self._record_funnel(
                    symbol=symbol,
                    side=direction.value,
                    stage="scanner_terminal",
                    accepted=False,
                    reason="mtf_analysis_error",
                )
                logger.warning(
                    "entry blocked: multi-timeframe confirmation unavailable: %s",
                    str(exc),
                    extra={
                        "aitos_extra": {
                            "symbol": symbol,
                            "entry_timeframe": self._timeframe,
                            "error": str(exc),
                        }
                    },
                )
                return None

        live_state = self._live_cache.snapshot(symbol)
        depth_events = (
            list(live_state.depth_event_history)
            if live_state is not None and market_source == "websocket_live_state"
            else []
        )
        depth_analysis = depth_event_flow_analysis(depth_events)
        ofi_score = (
            depth_analysis["ofi_score"] if depth_analysis["ofi_available"] else 5.0
        )
        ofi_directional_score = (
            ofi_score
            if direction == TradeSide.LONG
            else 10.0 - ofi_score if direction == TradeSide.SHORT else 5.0
        )
        if direction is not None and adverse_flow_without_progress(
            trades, direction.value
        ):
            await self._record_funnel(
                symbol=symbol,
                side=direction.value,
                stage="execution_quality_gate",
                accepted=False,
                reason="adverse_flow_without_price_progress",
                details={"market_source": market_source},
            )
            logger.info(
                "entry rejected: aggressive flow failed to produce price progress",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "direction": direction.value,
                        "market_source": market_source,
                    }
                },
            )
            await self._record_funnel(
                symbol=symbol,
                side=direction.value,
                stage="scanner_terminal",
                accepted=False,
                reason="adverse_flow_without_price_progress",
                details={"market_source": market_source},
            )
            return None
        if direction is not None:
            await self._record_funnel(
                symbol=symbol,
                side=direction.value,
                stage="execution_quality_gate",
                accepted=True,
                reason="flow_progress_confirmed",
                details={"market_source": market_source},
            )

        liquidity_quality = liquidity_quality_score(order_book)
        depth_imbalance = depth_imbalance_score(order_book)
        liquidity_wall = liquidity_wall_score(order_book)
        sweep_potential = sweep_potential_score(order_book)
        absorption = absorption_proxy_score(order_book, trades)
        liquidity_score = liquidity_intelligence_score(order_book, trades)
        orderflow_delta = flow_features.delta if flow_features else 0.0
        orderflow_cvd = flow_features.cvd if flow_features else 0.0
        orderflow_buy_ratio = flow_features.buy_ratio if flow_features else 0.5
        orderflow_aggression = flow_features.aggression if flow_features else 0.0
        orderflow_vwap = flow_features.vwap if flow_features else 0.0
        # Avoid chasing an already-extended impulse: compare the close with
        # the recent structural boundary in ATR units, not a fixed percentage.
        recent = klines[-7:-1]
        if recent and atr > 0:
            boundary = (
                max(float(k.high) for k in recent)
                if direction == TradeSide.LONG
                else min(float(k.low) for k in recent)
            )
            extension_atr = (
                ((float(klines[-1].close) - boundary) / atr)
                if direction == TradeSide.LONG
                else ((boundary - float(klines[-1].close)) / atr)
            )
            entry_timing_score = round(
                max(0.0, min(10.0, 8.0 - max(0.0, extension_atr) * 4.0)), 2
            )
        else:
            extension_atr = 0.0
            entry_timing_score = 5.0

        logger.info(
            "paper signal diagnostics",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "market_source": market_source,
                    "live_fresh": live_fresh,
                    "executed_trades": len(trades),
                    "structure": structure_direction,
                    "structure_strength": round(structure_strength, 2),
                    "structure_bias": market_structure.bias,
                    "structure_event": market_structure.event,
                    "candle_cvd": round(candle_cvd, 3),
                    "orderflow_bias": round(flow_score, 3),
                    "orderflow_delta": round(orderflow_delta, 6),
                    "orderflow_cvd": round(orderflow_cvd, 6),
                    "orderflow_buy_ratio": round(orderflow_buy_ratio, 4),
                    "orderflow_aggression": round(orderflow_aggression, 4),
                    "orderflow_vwap": round(orderflow_vwap, 6),
                    "liquidity_quality": liquidity_quality,
                    "depth_imbalance": depth_imbalance,
                    "liquidity_wall": liquidity_wall,
                    "sweep_potential": sweep_potential,
                    "absorption_proxy": absorption,
                    "liquidity_score": liquidity_score,
                    "order_book_flow_imbalance": ofi_directional_score,
                    "depth_event_count": depth_analysis["event_count"],
                    "depth_ofi_confidence": depth_analysis["ofi_confidence"],
                    "depth_ofi_available": depth_analysis["ofi_available"],
                    "depth_freshness_ms": depth_analysis["freshness_ms"],
                    "depth_fresh_event_count": depth_analysis["fresh_event_count"],
                    "depth_stale_event_count": depth_analysis["stale_event_count"],
                    "depth_inferred_cancel_qty": depth_analysis["inferred_cancel_qty"],
                    "depth_matched_execution_qty": depth_analysis[
                        "matched_execution_qty"
                    ],
                    "depth_attribution_pending_qty": depth_analysis[
                        "attribution_pending_qty"
                    ],
                    "depth_execution_attributed_ratio": depth_analysis[
                        "execution_attributed_ratio"
                    ],
                    "depth_cancellation_proxy_qty": depth_analysis[
                        "cancellation_proxy_qty"
                    ],
                    "depth_replenishment_qty": depth_analysis["replenishment_qty"],
                    "depth_bid_replenishment_qty": depth_analysis[
                        "bid_replenishment_qty"
                    ],
                    "depth_ask_replenishment_qty": depth_analysis[
                        "ask_replenishment_qty"
                    ],
                    "depth_bid_replenishment_ratio": depth_analysis[
                        "bid_replenishment_ratio"
                    ],
                    "depth_ask_replenishment_ratio": depth_analysis[
                        "ask_replenishment_ratio"
                    ],
                    "depth_replenishment_ratio": depth_analysis["replenishment_ratio"],
                    "depth_inferred_cancel_pressure": depth_analysis[
                        "inferred_cancel_pressure"
                    ],
                    "depth_bid_inferred_cancel_pressure": depth_analysis[
                        "bid_inferred_cancel_pressure"
                    ],
                    "depth_ask_inferred_cancel_pressure": depth_analysis[
                        "ask_inferred_cancel_pressure"
                    ],
                    "entry_extension_atr": round(extension_atr, 3),
                    "entry_timing_score": entry_timing_score,
                    "direction": direction.value if direction else "none",
                    "scanner_threshold": self._min_score_threshold,
                }
            },
        )
        if direction is None:
            await self._record_funnel(
                symbol=symbol,
                side="UNKNOWN",
                stage="scanner_terminal",
                accepted=False,
                reason="no_direction_selected",
                details={
                    "structure_direction": structure_direction,
                    "structure_bias": market_structure.bias,
                    "cvd_score": round(flow_score, 4),
                },
            )
            return None
        liquidity_gate = entry_liquidity_depth_rejection(
            direction=direction,
            liquidity_quality=liquidity_quality,
            depth_analysis=depth_analysis,
            min_liquidity_quality=self._tuning.scanner.min_liquidity_quality,
            max_directional_cancel_pressure=(
                self._tuning.scanner.max_directional_cancel_pressure
            ),
        )
        if liquidity_gate is not None:
            gate_reason, gate_details = liquidity_gate
            details = {
                "symbol": symbol,
                "market_source": market_source,
                "direction": direction.value,
                **gate_details,
            }
            await self._record_funnel(
                symbol=symbol,
                side=direction.value,
                stage="liquidity_depth_gate",
                accepted=False,
                reason=gate_reason,
                details=details,
            )
            await self._record_funnel(
                symbol=symbol,
                side=direction.value,
                stage="scanner_terminal",
                accepted=False,
                reason=gate_reason,
                details=details,
            )
            logger.info(
                "entry rejected by hard liquidity/depth guard",
                extra={"aitos_extra": details},
            )
            return None
        await self._record_funnel(
            symbol=symbol,
            side=direction.value,
            stage="liquidity_depth_gate",
            accepted=True,
            reason="liquidity_and_directional_depth_accepted",
            details={
                "liquidity_quality": liquidity_quality,
                "min_liquidity_quality": self._tuning.scanner.min_liquidity_quality,
                "ofi_available": depth_analysis["ofi_available"],
                "bid_inferred_cancel_pressure": depth_analysis[
                    "bid_inferred_cancel_pressure"
                ],
                "ask_inferred_cancel_pressure": depth_analysis[
                    "ask_inferred_cancel_pressure"
                ],
            },
        )
        adx_score = min(10.0, indicators.adx(klines) / 10.0)
        structure_boost = min(2.0, structure_strength * 0.2)
        if (
            market_structure.bias == "bullish"
            and direction == TradeSide.LONG
            or market_structure.bias == "bearish"
            and direction == TradeSide.SHORT
        ):
            trend_score = min(10.0, adx_score + structure_boost)
        elif market_structure.event == "choch":
            trend_score = max(0.0, adx_score - 0.5)
        else:
            trend_score = adx_score
        momentum = indicators.momentum_score(klines)
        trend_score = round(min(10.0, 0.85 * trend_score + 0.15 * momentum), 2)
        volatility_score = _volatility_fitness(vol_percentile)
        regime_score = REGIME_FIT_SCORE.get(regime, 5.0)
        lead_lag_available = bool(reference_klines) and symbol != self._reference_symbol
        lead_lag = (
            indicators.lead_lag_score(klines, reference_klines)
            if lead_lag_available
            else 5.0
        )
        relative_strength = (
            relative_strength_score(klines, reference_klines, direction.value)
            if lead_lag_available
            else 5.0
        )
        breakout_acceptance = (
            breakout_acceptance_score(klines, direction.value, atr=atr)
            if structure_direction in {"bullish_bos", "bearish_bos"}
            else 5.0
        )
        funding_score = funding_rate_score(funding, direction)
        price_moved_up = klines[-1].close > klines[0].close
        oi_score = oi_trend_score(oi_current, oi_previous, direction, price_moved_up)
        candle_auction = auction_context_score(klines, direction.value)
        live_auction = (
            live_auction_score(trades, order_book, direction.value)
            if live_fresh
            else 5.0
        )
        amt_context = self._analyze_amt(symbol, trades, klines, order_book)
        if amt_context is not None:
            location_score = (
                5.0 + (amt_context.price_location - 0.5) * 4.0
                if direction == TradeSide.LONG
                else 5.0 - (amt_context.price_location - 0.5) * 4.0
            )
            state_bias = {
                "trend": 1.0,
                "discovery_up": 0.8,
                "discovery_down": -0.8,
                "acceptance": 0.5,
                "rejection": -0.5,
                "balance": 0.0,
                "rotation": 0.0,
                "unknown": 0.0,
            }.get(amt_context.state.value, 0.0)
            state_bias = -state_bias if direction == TradeSide.SHORT else state_bias
            amt_score = round(
                max(
                    0.0,
                    min(
                        10.0,
                        location_score
                        + state_bias
                        + (amt_context.acceptance - amt_context.rejection),
                    ),
                ),
                2,
            )
        else:
            amt_score = live_auction if live_fresh else candle_auction
        tracker = self._liquidity_trackers.setdefault(symbol, LiquidityTracker())
        liquidity_events = tracker.update(order_book, trades)
        tick_size = self._footprint_tick_size(symbol)
        footprint_available = False
        if tick_size is not None and trades:
            footprint = self._footprint_engines.setdefault(
                symbol, FootprintEngine(tick_size)
            ).build(trades)
            footprint_signals = self._footprint_signal_engine.evaluate(footprint)
            interaction = self._interaction_engine.evaluate(
                footprint_signals, liquidity_events
            )
            footprint_available = True
            if interaction.direction != "neutral" and interaction.score > 0:
                interaction_score = (
                    5.0 + interaction.score * 0.5
                    if interaction.direction == direction.value
                    else max(0.0, 5.0 - interaction.score * 0.5)
                )
            elif footprint_signals.bias == "bullish":
                interaction_score = (
                    footprint_signals.delta_score + footprint_signals.imbalance_score
                ) / 2.0
                if direction == TradeSide.SHORT:
                    interaction_score = 10.0 - interaction_score
            elif footprint_signals.bias == "bearish":
                interaction_score = (
                    footprint_signals.delta_score + footprint_signals.imbalance_score
                ) / 2.0
                if direction == TradeSide.LONG:
                    interaction_score = 10.0 - interaction_score
            else:
                interaction_score = 5.0
                footprint_available = False
            interaction_score = round(min(10.0, max(0.0, interaction_score)), 2)
            interaction_rationale = (
                f"footprint={footprint_signals.bias}, interaction={interaction.kind}, "
                f"interaction_dir={interaction.direction}, "
                f"interaction_score={interaction_score:.1f}, tick_size={tick_size:g}, "
                f"available={footprint_available}"
            )
        else:
            interaction_score, interaction_rationale = (
                5.0,
                "footprint=not_configured; interaction=unavailable",
            )
            footprint_available = False
        rl_context = {
            "regime": regime,
            "direction": direction.value,
            "htf_trend_alignment": htf_alignment_score,
            "htf_trend": htf_diagnostics.get("trend", "disabled"),
            "trend_strength": trend_score,
            "liquidity_quality": liquidity_score,
            "order_flow_bias": flow_score,
            "auction_context": amt_score,
            "volatility": volatility_score,
            "market_regime": regime_score,
            "lead_lag": lead_lag,
            "funding_rate": funding_score,
            "open_interest_trend": oi_score,
            "footprint_interaction": interaction_score,
            "order_book_flow_imbalance": ofi_directional_score,
            "order_book_flow_imbalance_confidence": depth_analysis["ofi_confidence"],
            "depth_freshness_ms": depth_analysis["freshness_ms"],
            "depth_inferred_cancel_pressure": depth_analysis[
                "inferred_cancel_pressure"
            ],
            "depth_bid_inferred_cancel_pressure": depth_analysis[
                "bid_inferred_cancel_pressure"
            ],
            "depth_ask_inferred_cancel_pressure": depth_analysis[
                "ask_inferred_cancel_pressure"
            ],
            "depth_matched_execution_qty": depth_analysis["matched_execution_qty"],
            "depth_attribution_pending_qty": depth_analysis["attribution_pending_qty"],
            "depth_execution_attributed_ratio": depth_analysis[
                "execution_attributed_ratio"
            ],
            "depth_matched_bid_execution_qty": depth_analysis[
                "matched_bid_execution_qty"
            ],
            "depth_matched_ask_execution_qty": depth_analysis[
                "matched_ask_execution_qty"
            ],
            "depth_bid_cancellation_proxy_qty": depth_analysis[
                "bid_cancellation_proxy_qty"
            ],
            "depth_ask_cancellation_proxy_qty": depth_analysis[
                "ask_cancellation_proxy_qty"
            ],
            "depth_matched_trade_count": depth_analysis["matched_trade_count"],
            "depth_cancellation_proxy_qty": depth_analysis["cancellation_proxy_qty"],
            "depth_replenishment_ratio": depth_analysis["replenishment_ratio"],
            "depth_bid_replenishment_qty": depth_analysis["bid_replenishment_qty"],
            "depth_ask_replenishment_qty": depth_analysis["ask_replenishment_qty"],
            "depth_bid_replenishment_ratio": depth_analysis["bid_replenishment_ratio"],
            "depth_ask_replenishment_ratio": depth_analysis["ask_replenishment_ratio"],
            "entry_timing": entry_timing_score,
            "breakout_acceptance": breakout_acceptance,
            "relative_strength": relative_strength,
        }
        if amt_context is not None:
            rl_context.update(
                {
                    "amt_acceptance": amt_context.acceptance,
                    "amt_rejection": amt_context.rejection,
                    "amt_price_location": amt_context.price_location,
                    "amt_confidence": amt_context.confidence,
                }
            )
        rl_score = await self._rl_scorer.score(symbol, rl_context)
        rl_available = abs(float(rl_score) - 5.0) > 1e-9 or not isinstance(
            self._rl_scorer, NeutralRLScorer
        )
        if rl_available and abs(float(rl_score) - 5.0) < 1e-9:
            sample_count = getattr(self._rl_scorer, "sample_count", None)
            if callable(sample_count):
                try:
                    if sample_count(symbol, regime, direction.value) == 0:
                        rl_available = False
                except Exception:
                    pass
        a_stat_score, a_stat_result = await asyncio.to_thread(
            self._a_stat_bridge.evaluate,
            symbol=symbol,
            direction=direction,
            klines=klines,
            flow_score=flow_score,
            candle_cvd=candle_cvd,
            orderflow_vwap=orderflow_vwap,
            depth_imbalance=depth_imbalance,
            funding_score=funding_score,
            oi_score=oi_score,
            lead_lag=lead_lag,
            last_close=float(klines[-1].close),
        )
        a_stat_component = a_stat_score if a_stat_score is not None else 5.0
        component_scores = {
            "trend_strength": round(trend_score, 2),
            "liquidity_quality": liquidity_score,
            "order_flow_bias": flow_score,
            "auction_context": amt_score,
            "volatility": volatility_score,
            "market_regime": regime_score,
            "lead_lag": lead_lag,
            "funding_rate": funding_score,
            "open_interest_trend": oi_score,
            "rl_confidence": round(rl_score, 2),
            "footprint_interaction": interaction_score,
            "a_stat": a_stat_component,
            "order_book_flow_imbalance": ofi_directional_score,
            "entry_timing": entry_timing_score,
            "breakout_acceptance": breakout_acceptance,
            "relative_strength": relative_strength,
            "htf_trend_alignment": htf_alignment_score,
        }
        component_availability = {
            "trend_strength": True,
            "liquidity_quality": True,
            "order_flow_bias": True,
            "auction_context": True,
            "volatility": True,
            "market_regime": regime != "unknown",
            "lead_lag": lead_lag_available,
            "funding_rate": True,
            "open_interest_trend": oi_previous is not None,
            "rl_confidence": rl_available,
            "footprint_interaction": footprint_available,
            "a_stat": a_stat_score is not None,
            "order_book_flow_imbalance": depth_analysis["ofi_available"],
            "entry_timing": atr > 0,
            "breakout_acceptance": structure_direction
            in {"bullish_bos", "bearish_bos"},
            "relative_strength": lead_lag_available,
            "htf_trend_alignment": htf_available,
        }
        weight_total = sum(
            self._weights.get(k, 0.0)
            for k, available in component_availability.items()
            if available and k in component_scores
        )
        composite = (
            sum(
                component_scores[k] * self._weights.get(k, 0.0)
                for k, available in component_availability.items()
                if available and k in component_scores
            )
            / weight_total
            * 10
            if weight_total
            else 0.0
        )
        rationale = [
            f"{k.replace('_',' ')}={v:.1f}/10" for k, v in component_scores.items()
        ]
        rationale.append(
            f"executed_trades={len(trades)}, candle_cvd={candle_cvd:.1f}, live_auction={live_auction:.1f}, candle_auction={candle_auction:.1f}, amt_score={amt_score:.1f}, regime={regime}, structure={structure_direction}, direction={direction.value}, market_source={market_source}, ofi={ofi_score:.2f}, ofi_confidence={depth_analysis['ofi_confidence']:.3f}, ofi_freshness_ms={depth_analysis['freshness_ms']:.1f}, inferred_cancel_pressure={depth_analysis['inferred_cancel_pressure']:.3f}, replenishment_ratio={depth_analysis['replenishment_ratio']:.3f}, entry_extension_atr={extension_atr:.3f}"
        )
        if amt_context is not None:
            rationale.extend(
                [
                    f"amt_state={amt_context.state.value}, day_type={amt_context.day_type.value}, confidence={amt_context.confidence:.3f}",
                    f"amt_poc={amt_context.poc:g}, vah={amt_context.vah:g}, val={amt_context.val:g}, ib={amt_context.ib_low:g}-{amt_context.ib_high:g}",
                    f"amt_migration={amt_context.value_migration.value}, acceptance={amt_context.acceptance:.3f}, rejection={amt_context.rejection:.3f}",
                ]
            )
        if flow_features:
            rationale.append(
                f"orderflow delta={flow_features.delta:.4f}, cvd={flow_features.cvd:.4f}, buy_ratio={flow_features.buy_ratio:.3f}, aggression={flow_features.aggression:.3f}, vwap={flow_features.vwap:.4f}"
            )
        rationale.extend(
            [
                interaction_rationale,
                f"liquidity=orderbook+tradeflow, structure_strength={structure_strength:.1f}",
                f"structure_bias={market_structure.bias}, structure_event={market_structure.event}, last_swing={market_structure.last_swing_type}",
                f"regime={regime}",
                f"htf={htf_diagnostics.get('timeframe', 'disabled')}:{htf_diagnostics.get('trend', 'disabled')} "
                f"adx={float(htf_diagnostics.get('adx', 0.0)):.1f} "
                f"di_gap={float(htf_diagnostics.get('di_gap', 0.0)):.1f} "
                f"reason={htf_diagnostics.get('reason', 'not_evaluated')}",
                f"breakout_acceptance={breakout_acceptance:.1f}, relative_strength={relative_strength:.1f}",
                f"unavailable_features={[k for k, ok in component_availability.items() if not ok]}",
            ]
        )
        if a_stat_result is not None:
            rationale.append(
                f"a_stat p_up={a_stat_result.direction.up:.3f}, "
                f"p_down={a_stat_result.direction.down:.3f}, "
                f"ev={a_stat_result.expected_value:.6f}, "
                f"vol={a_stat_result.expected_volatility:.6f}, "
                f"tail={a_stat_result.tail_loss_probability:.3f}, "
                f"confidence={a_stat_result.probability_confidence:.3f}, "
                f"advanced_stack={a_stat_result.advanced is not None}"
            )
        scanned_at = _utc_now_iso()
        statistical_forecast = None
        if a_stat_result is not None and a_stat_result.advanced is not None:
            advanced = a_stat_result.advanced
            is_long = direction == TradeSide.LONG
            loss_tail = advanced.evt if is_long else advanced.upside_evt
            reference_variance = float(advanced.garch.long_run_variance)
            forecast_volatility = float(a_stat_result.expected_volatility)
            if (
                loss_tail is not None
                and isfinite(reference_variance)
                and reference_variance > 0.0
                and isfinite(forecast_volatility)
                and forecast_volatility >= 0.0
            ):
                statistical_forecast = {
                    "symbol": symbol,
                    "side": direction.value,
                    "horizon": self._timeframe,
                    "model_id": "aitos-contract-statistical-stack",
                    "model_version": "1",
                    "as_of": scanned_at,
                    "window_start": _timestamp_iso(
                        getattr(klines[0], "open_time", None), scanned_at
                    ),
                    "feature_snapshot_id": (f"{symbol}:{self._timeframe}:{scanned_at}"),
                    "sample_size": int(a_stat_result.sample_size),
                    "predicted_up": float(
                        a_stat_result.direction.up
                        if is_long
                        else a_stat_result.direction.down
                    ),
                    "downside_probability": float(
                        a_stat_result.downside_probability
                        if is_long
                        else a_stat_result.direction.up
                    ),
                    "forecast_volatility": forecast_volatility,
                    "reference_volatility": reference_variance**0.5,
                    "tail_probability": float(loss_tail.tail_probability),
                    "expected_shortfall": float(loss_tail.expected_shortfall),
                    "expected_return": float(
                        a_stat_result.expected_return * (1.0 if is_long else -1.0)
                    ),
                    "probability_confidence": float(
                        a_stat_result.probability_confidence
                    ),
                }
        candidate = ScanCandidate(
            symbol=symbol,
            direction=direction,
            composite_score=round(composite, 2),
            component_scores=component_scores,
            component_availability=component_availability,
            rationale=rationale,
            entry_price=klines[-1].close,
            atr=atr,
            regime=regime,
            scanned_at=scanned_at,
            htf_adx=float(htf_diagnostics.get("adx", 0.0)),
            trace_id=_ACTIVE_SCAN_TRACE_ID.get() or None,
            statistical_forecast=statistical_forecast,
        )
        event_features = self._financial_ml.event_features(symbol)
        from aitos.statistics.financial_volatility import (
            financial_volatility_features,
        )

        closes = [float(kline.close) for kline in klines]
        historical_returns = [
            log(closes[index] / closes[index - 1])
            for index in range(1, len(closes))
            if closes[index - 1] > 0 and closes[index] > 0
        ]
        volatility_features = financial_volatility_features(historical_returns)
        financial_ml_features = {
            "composite_score": candidate.composite_score,
            "trend_strength": component_scores["trend_strength"],
            "liquidity_quality": component_scores["liquidity_quality"],
            "order_flow_bias": component_scores["order_flow_bias"],
            "auction_context": component_scores["auction_context"],
            "volatility_score": component_scores["volatility"],
            "market_regime": component_scores["market_regime"],
            "lead_lag": component_scores["lead_lag"],
            "funding_rate": component_scores["funding_rate"],
            "open_interest_trend": component_scores["open_interest_trend"],
            "rl_confidence": component_scores["rl_confidence"],
            "footprint_interaction": component_scores["footprint_interaction"],
            "a_stat": component_scores["a_stat"],
            "order_book_flow_imbalance": component_scores["order_book_flow_imbalance"],
            "entry_timing": component_scores["entry_timing"],
            "breakout_acceptance": component_scores["breakout_acceptance"],
            "relative_strength": component_scores["relative_strength"],
            "atr_pct": (
                float(atr) / float(candidate.entry_price)
                if candidate.entry_price > 0
                else 0.0
            ),
            "side_sign": (1.0 if candidate.direction.value == "LONG" else -1.0),
            **event_features,
            **volatility_features,
        }
        from dataclasses import replace as replace_candidate

        candidate = replace_candidate(
            candidate, financial_ml_features=financial_ml_features
        )
        try:
            await asyncio.to_thread(
                self._financial_ml.register_candidate,
                candidate_id=candidate.candidate_id,
                symbol=symbol,
                side=direction.value,
                entry_price=float(candidate.entry_price),
                volatility=max(1e-6, financial_ml_features["atr_pct"]),
                features=financial_ml_features,
                created_at=scanned_at,
                trace_id=candidate.trace_id,
            )
        except Exception:
            logger.exception(
                "financial ML candidate registration failed",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "candidate_id": candidate.candidate_id,
                    }
                },
            )
        await self._record_funnel(
            symbol=symbol,
            side=direction.value,
            stage="scanner_candidate",
            accepted=True,
            reason="candidate_scored",
            details={
                "composite_score": candidate.composite_score,
                "component_availability": candidate.component_availability,
                "regime": candidate.regime,
                "candidate_id": candidate.candidate_id,
            },
        )
        return candidate

    async def _lightweight_rank_symbols(self, limit: int | None = None) -> list[str]:
        """Rank the configured universe cheaply before invoking full intelligence.

        This stage only requests a short candle window. It intentionally does not
        fetch order books, recent trades, funding, or open interest. Failed
        lightweight requests retain a deterministic fallback rank so temporary
        data-source issues do not crash the scan cycle.
        """
        limit = (
            self._tuning.analysis.expensive_analysis_limit if limit is None else limit
        )

        semaphore = asyncio.Semaphore(self._tuning.analysis.lightweight_concurrency)

        async def _score(symbol: str, index: int) -> tuple[str, float, int]:
            async with semaphore:
                try:
                    candles = await asyncio.wait_for(
                        self._exchange.fetch_klines(symbol, self._timeframe, limit=20),
                        timeout=self._tuning.analysis.timeout_seconds,
                    )
                    candles = [c for c in candles if getattr(c, "is_closed", True)]
                    if len(candles) < 5:
                        return symbol, -1.0, index
                    first_close = float(candles[0].close)
                    last_close = float(candles[-1].close)
                    if first_close <= 0 or last_close <= 0:
                        return symbol, -1.0, index
                    # Rank by executable activity rather than chasing the
                    # largest recent price move. Quote-volume proxy is close *
                    # base volume; median volume dampens one-candle spikes.
                    volumes = sorted(
                        float(c.volume) * float(c.close)
                        for c in candles
                        if float(c.volume) > 0 and float(c.close) > 0
                    )
                    if not volumes:
                        return symbol, -1.0, index
                    median_quote_activity = volumes[len(volumes) // 2]
                    closes = [float(c.close) for c in candles[-6:]]
                    previous_close = closes[-2] if len(closes) >= 2 else closes[-1]
                    short_return = (
                        closes[-1] / previous_close - 1.0 if previous_close > 0 else 0.0
                    )
                    self._lightweight_fingerprints[symbol] = (
                        f"{round(closes[-1], 10)}:{round(short_return, 5)}:"
                        f"{round(median_quote_activity, 2)}"
                    )
                    spread_range = (
                        max(float(c.high) - float(c.low) for c in candles) / last_close
                    )
                    stability = 1.0 / (1.0 + max(0.0, spread_range) * 100.0)
                    return symbol, median_quote_activity * stability, index
                except Exception as exc:
                    logger.debug(
                        "lightweight scanner screen failed; preserving fallback rank",
                        extra={
                            "aitos_extra": {
                                "symbol": symbol,
                                "error": str(exc),
                            }
                        },
                    )
                    return symbol, -1.0, index

        scores = await asyncio.gather(
            *(_score(symbol, index) for index, symbol in enumerate(self._symbols))
        )
        ranked = sorted(scores, key=lambda item: (-item[1], item[2]))
        selected = []
        cooldown_skipped = 0
        for symbol, _, _ in ranked:
            fingerprint = self._lightweight_fingerprints.get(symbol, "unavailable")
            if self._candidate_rotation.should_skip(symbol, fingerprint):
                cooldown_skipped += 1
                continue
            selected.append(symbol)
            if len(selected) >= limit:
                break
        logger.info(
            "lightweight scanner screening complete",
            extra={
                "aitos_extra": {
                    "universe_size": len(self._symbols),
                    "deep_analysis_budget": limit,
                    "selected_symbols": selected,
                    "screened_symbols": len(scores),
                    "cooldown_skipped": cooldown_skipped,
                    "candidate_rotation": self._candidate_rotation.snapshot(),
                }
            },
        )
        return selected

    async def scan_all(self) -> list[ScanCandidate]:
        self._require_initialized()
        reference_klines = None
        if self._reference_symbol:
            try:
                reference_klines = await self._exchange.fetch_klines(
                    self._reference_symbol, self._timeframe, limit=self._kline_lookback
                )
            except Exception as exc:
                logger.error("failed to fetch reference symbol klines: %s", exc)

        selected_symbols = await self._lightweight_rank_symbols()

        async def _scan_one(symbol: str) -> ScanCandidate | None:
            async with self._scan_semaphore:
                try:
                    return await asyncio.wait_for(
                        self.scan_symbol(symbol, reference_klines),
                        timeout=self._tuning.analysis.timeout_seconds,
                    )
                except Exception as exc:
                    logger.error(
                        "scan failed for symbol",
                        extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                    )
                    return None

        results = await asyncio.gather(
            *(_scan_one(symbol) for symbol in selected_symbols)
        )
        candidates = [candidate for candidate in results if candidate is not None]
        self._last_scan_at, self._last_candidate_count = _utc_now_iso(), len(candidates)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_SCAN_COMPLETE,
                payload={
                    "universe_symbols": len(self._symbols),
                    "symbols_screened": len(self._symbols),
                    "symbols_scanned": len(selected_symbols),
                    "deep_analysis_budget": self._tuning.analysis.expensive_analysis_limit,
                    "candidates_found": len(candidates),
                },
                source_module=self.module_id,
            )
        )
        return candidates

    @property
    def target_opportunities(self) -> int:
        return self._top_n

    def record_candidate_rejection(self, candidate: ScanCandidate, reason: str) -> None:
        self._financial_ml.reject_candidate(candidate.candidate_id, reason)
        rejection_id = self._candidate_outcomes.record_rejection(candidate, reason)
        attribution_id = candidate.candidate_id or rejection_id
        if rejection_id:
            try:
                self._decision_outcome_ledger.record_decision(
                    f"{rejection_id}:REJECTION",
                    entity_id=attribution_id,
                    symbol=candidate.symbol,
                    side=candidate.direction.value,
                    decision_type="REJECTION",
                    accepted=False,
                    reason=reason,
                    payload={
                        "rejection_id": rejection_id,
                        "trace_id": candidate.trace_id,
                        "composite_score": candidate.composite_score,
                        "regime": candidate.regime,
                        "candidate_id": candidate.candidate_id,
                    },
                    candidate_id=candidate.candidate_id,
                )
            except Exception:
                logger.exception(
                    "candidate rejection attribution persistence failed",
                    extra={
                        "aitos_extra": {
                            "symbol": candidate.symbol,
                            "trace_id": candidate.trace_id,
                        }
                    },
                )
        self._candidate_rotation.record_rejection(
            candidate.symbol,
            reason,
            self._lightweight_fingerprints.get(
                candidate.symbol,
                f"{candidate.direction.value}:{candidate.regime}:{candidate.composite_score:.2f}",
            ),
        )

    def _reconcile_financial_ml_labels(self) -> int:
        """Publish resolved counterfactual barrier labels with their exact resolution time."""
        resolved = 0
        for label in self._financial_ml.pending_counterfactual_labels():
            candidate_id = str(label["candidate_id"])
            try:
                updated = self._decision_outcome_ledger.resolve_entity(
                    candidate_id,
                    outcome_value=float(label["net_return"]),
                    outcome_at=str(label["label_resolution_time"]),
                    outcome={
                        **dict(label.get("outcome") or {}),
                        "label": int(label["label"]),
                        "counterfactual": True,
                        "provenance": str(label["outcome_provenance"]),
                    },
                    candidate_id=candidate_id,
                    outcome_provenance=str(label["outcome_provenance"]),
                )
                if updated:
                    self._financial_ml.mark_ledger_synced(candidate_id)
                    resolved += updated
                else:
                    existing = self._decision_outcome_ledger.list_decisions(
                        entity_id=candidate_id
                    )
                    if existing and all(
                        row.get("outcome_value") is not None for row in existing
                    ):
                        self._financial_ml.mark_ledger_synced(candidate_id)
            except Exception:
                logger.exception(
                    "financial ML candidate label could not be synchronized to decision ledger",
                    extra={"aitos_extra": {"candidate_id": candidate_id}},
                )
        return resolved

    def _reconcile_candidate_markouts(self) -> int:
        """Resolve rejected-candidate decisions from the final 15-minute markout."""
        resolved = 0
        for markout in self._candidate_outcomes.completed_markouts(horizon_seconds=900):
            attribution_id = str(markout.get("attribution_id") or "")
            if not attribution_id:
                continue
            if self._financial_ml.has_candidate(str(attribution_id)):
                continue
            existing = self._decision_outcome_ledger.list_decisions(
                entity_id=attribution_id
            )
            if any(row.get("outcome_value") is not None for row in existing):
                continue
            observed_at = datetime.fromtimestamp(
                float(markout["observed_at"]), tz=timezone.utc
            ).isoformat()
            try:
                resolved += self._decision_outcome_ledger.resolve_entity(
                    attribution_id,
                    outcome_value=float(markout["signed_return_pct"]) / 100.0,
                    outcome_at=observed_at,
                    outcome={
                        "counterfactual": True,
                        "provenance": "COUNTERFACTUAL_REJECTED_CANDIDATE_MARKOUT",
                        "horizon_seconds": 900,
                        "markout_price": float(markout["price"]),
                        "mae_pct": float(markout["mae_pct"]),
                        "mfe_pct": float(markout["mfe_pct"]),
                        "rejection_reason": str(markout["reason"]),
                    },
                    candidate_id=str(markout.get("candidate_id") or attribution_id),
                    outcome_provenance="COUNTERFACTUAL_REJECTED_CANDIDATE_MARKOUT",
                )
            except Exception:
                logger.exception(
                    "rejected-candidate outcome reconciliation failed",
                    extra={
                        "aitos_extra": {
                            "symbol": markout.get("symbol"),
                            "rejection_id": markout.get("rejection_id"),
                        }
                    },
                )
        return resolved

    def clear_candidate_cooldown(self, symbol: str) -> None:
        self._candidate_rotation.force_recheck(symbol)

    async def iter_candidate_batches(self):
        """Analyse bounded batches, yielding each to downstream gates before refill."""
        self._require_initialized()
        reference_klines = None
        if self._reference_symbol:
            try:
                reference_klines = await self._exchange.fetch_klines(
                    self._reference_symbol, self._timeframe, limit=self._kline_lookback
                )
            except Exception as exc:
                logger.error("failed to fetch reference symbol klines: %s", exc)
        cfg = self._tuning.analysis
        selected = await self._lightweight_rank_symbols(limit=cfg.candidate_pool_limit)
        budget = min(cfg.max_candidate_analyses_per_cycle, cfg.candidate_pool_limit)
        selected = selected[:budget]
        scanned = found = 0
        batch_size = max(1, cfg.candidate_batch_size)
        for offset in range(0, len(selected), batch_size):
            batch = selected[offset : offset + batch_size]

            async def scan_one(symbol: str) -> ScanCandidate | None:
                async with self._scan_semaphore:
                    try:
                        return await asyncio.wait_for(
                            self.scan_symbol(symbol, reference_klines),
                            timeout=cfg.timeout_seconds,
                        )
                    except Exception as exc:
                        for rejected_side in ("LONG", "SHORT"):
                            await self._record_funnel(
                                symbol=symbol,
                                side=rejected_side,
                                stage="scanner_batch",
                                accepted=False,
                                reason=(
                                    "scan_timeout"
                                    if isinstance(exc, asyncio.TimeoutError)
                                    else "scan_exception"
                                ),
                                details={"error": str(exc)[:500]},
                            )
                        logger.error(
                            "candidate batch scan failed",
                            extra={
                                "aitos_extra": {"symbol": symbol, "error": str(exc)}
                            },
                        )
                        return None

            results = await asyncio.gather(*(scan_one(symbol) for symbol in batch))
            scanned += len(batch)
            candidates = [item for item in results if item is not None]
            ranked = await self.rank(candidates, top_n=len(candidates))
            found += len(ranked)
            self._last_scan_at, self._last_candidate_count = _utc_now_iso(), found
            payload = {
                "phase": "batch_completed",
                "batch_index": offset // batch_size + 1,
                "batch_size": len(batch),
                "symbols_scanned": scanned,
                "analysis_budget": budget,
                "candidates_found": found,
                "ranked_symbols": [item.symbol for item in ranked],
                "rotation": self._candidate_rotation.snapshot(),
            }
            logger.info(
                "candidate analysis batch completed", extra={"aitos_extra": payload}
            )
            await self._event_bus.publish(
                Event(
                    topic="market.candidate_rotation",
                    payload=payload,
                    source_module=self.module_id,
                )
            )
            yield ranked

    async def rank(
        self, candidates: list[ScanCandidate], top_n: int | None = None
    ) -> list[ScanCandidate]:
        n = top_n if top_n is not None else self._top_n
        from dataclasses import replace as replace_candidate

        # The scanner is normally fully initialized, but lightweight unit
        # harnesses and diagnostic callers may construct it without the
        # optional FinancialMLPipeline. In that case preserve the primary
        # strategy's score/ranking behavior rather than crashing; production
        # construction always installs the pipeline in __init__.
        financial_ml = getattr(self, "_financial_ml", None)
        scored: list[ScanCandidate] = []
        for candidate in candidates:
            if financial_ml is None:
                scored.append(candidate)
                continue
            prediction = financial_ml.predict(
                candidate.candidate_id, candidate.financial_ml_features
            )
            candidate = replace_candidate(
                candidate,
                meta_raw_probability=prediction.raw_probability,
                meta_probability=prediction.calibrated_probability,
                meta_accepted=(
                    prediction.accepted
                    if prediction.calibrated_probability is not None
                    else None
                ),
                meta_reason=prediction.reason,
            )
            scored.append(candidate)
            if prediction.calibrated_probability is not None:
                await self._record_funnel(
                    symbol=candidate.symbol,
                    side=candidate.direction.value,
                    stage="financial_ml_meta_gate",
                    accepted=prediction.accepted,
                    reason=prediction.reason,
                    details={
                        "candidate_id": candidate.candidate_id,
                        "raw_probability": prediction.raw_probability,
                        "calibrated_probability": prediction.calibrated_probability,
                        "mode": prediction.mode,
                        "enforced": financial_ml.should_block(prediction),
                    },
                )
        eligible = [
            candidate
            for candidate in scored
            if candidate.composite_score >= self._min_score_threshold
            and not (
                financial_ml is not None
                and financial_ml.enforce
                and financial_ml.should_block(
                    financial_ml.predict(
                        candidate.candidate_id, candidate.financial_ml_features
                    )
                )
            )
        ]
        eligible_symbols = {candidate.symbol for candidate in eligible}
        for candidate in scored:
            accepted = candidate.symbol in eligible_symbols
            await self._record_funnel(
                symbol=candidate.symbol,
                side=candidate.direction.value,
                stage="scanner_score_gate",
                accepted=accepted,
                reason=(
                    "score_above_threshold"
                    if accepted
                    else (
                        "financial_ml_meta_rejected"
                        if candidate.composite_score >= self._min_score_threshold
                        and financial_ml is not None
                        and financial_ml.enforce
                        else "composite_score_below_threshold"
                    )
                ),
                details={
                    "composite_score": candidate.composite_score,
                    "minimum_score": self._min_score_threshold,
                    "trace_id": candidate.trace_id,
                    "candidate_id": candidate.candidate_id,
                    "meta_probability": candidate.meta_probability,
                },
            )
        ranked = self._ranking_hysteresis.rank(eligible, n)
        ranked_symbols = {candidate.symbol for candidate in ranked}
        for candidate in eligible:
            accepted = candidate.symbol in ranked_symbols
            if not accepted and financial_ml is not None:
                await asyncio.to_thread(
                    financial_ml.reject_candidate,
                    candidate.candidate_id,
                    "excluded_by_top_n_or_hysteresis",
                )
            await self._record_funnel(
                symbol=candidate.symbol,
                side=candidate.direction.value,
                stage="ranking_gate",
                accepted=accepted,
                reason=(
                    "selected_for_execution_queue"
                    if accepted
                    else "excluded_by_top_n_or_hysteresis"
                ),
                details={
                    "composite_score": candidate.composite_score,
                    "top_n": n,
                    "trace_id": candidate.trace_id,
                    "candidate_id": candidate.candidate_id,
                    "meta_probability": candidate.meta_probability,
                },
            )
        for candidate in scored:
            if (
                financial_ml is not None
                and candidate.symbol not in ranked_symbols
                and candidate not in eligible
            ):
                await asyncio.to_thread(
                    financial_ml.reject_candidate,
                    candidate.candidate_id,
                    "score_or_meta_gate_rejection",
                )
        return ranked

    async def decide_with_kernel(self, candidate: ScanCandidate, kernel: Any) -> Any:
        from aitos.kernel.ai_kernel import DecisionContext

        return await kernel.request_decision(
            DecisionContext(
                symbol=candidate.symbol,
                context={
                    "direction": candidate.direction.value,
                    "component_scores": candidate.component_scores,
                    "component_availability": dict(candidate.component_availability),
                    "regime": candidate.regime,
                    "entry_price": candidate.entry_price,
                    "candidate_id": candidate.candidate_id,
                    "financial_ml_meta": {
                        "raw_probability": candidate.meta_raw_probability,
                        "calibrated_probability": candidate.meta_probability,
                        "accepted": candidate.meta_accepted,
                        "reason": candidate.meta_reason,
                    },
                },
            )
        )

    def to_opportunity(
        self,
        candidate: ScanCandidate,
        risk_reward_multiples: tuple[float, ...] | None = None,
        atr_stop_multiplier: float = 1.5,
        strategy_id: str = "opportunity-scanner",
        is_production: bool = False,
        approved_by: str | None = None,
    ) -> Opportunity:
        entry = candidate.entry_price
        # Keep the first two partial targets conservative while allowing the
        # final runner target to expand with a confirmed HTF trend. Explicit
        # caller-supplied multiples always take precedence.
        if risk_reward_multiples is None:
            if candidate.htf_adx > 35.0:
                risk_reward_multiples = (1.0, 2.0, 3.3)
            elif candidate.htf_adx >= 25.0:
                risk_reward_multiples = (1.0, 2.0, 2.5)
            else:
                risk_reward_multiples = (1.0, 2.0, 3.0)
        stop_distance = (
            candidate.atr * atr_stop_multiplier if candidate.atr > 0 else entry * 0.01
        )
        if candidate.direction == TradeSide.LONG:
            stop_loss_price, take_profit_levels = entry - stop_distance, [
                entry + stop_distance * r for r in risk_reward_multiples
            ]
        else:
            stop_loss_price, take_profit_levels = entry + stop_distance, [
                entry - stop_distance * r for r in risk_reward_multiples
            ]
        return Opportunity(
            symbol=candidate.symbol,
            side=candidate.direction,
            entry_price=entry,
            stop_loss_price=stop_loss_price,
            take_profit_levels=take_profit_levels,
            confidence=round(candidate.composite_score / 100.0, 4),
            strategy_id=strategy_id,
            rationale="; ".join(candidate.rationale),
            agent_consensus={
                **dict(candidate.component_scores),
                "trace_id": candidate.trace_id,
                "candidate_id": candidate.candidate_id,
                "financial_ml_meta": {
                    "raw_probability": candidate.meta_raw_probability,
                    "calibrated_probability": candidate.meta_probability,
                    "accepted": candidate.meta_accepted,
                    "reason": candidate.meta_reason,
                    "mode": self._financial_ml.mode,
                    "features": dict(candidate.financial_ml_features),
                },
                **(
                    {"statistical_forecast": dict(candidate.statistical_forecast)}
                    if candidate.statistical_forecast is not None
                    else {}
                ),
            },
            is_production=is_production,
            approved_by=approved_by,
            trailing_sl_enabled=True,
            regime=candidate.regime,
            timeframe=self._timeframe,
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "OpportunityScanner.initialize() must be called first"
            )
