"""OpportunityScanner — multi-source market intelligence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

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
from aitos.intelligence.amt.engine import AMTContext, AMTEngine
from aitos.intelligence.auction import auction_context_score
from aitos.intelligence.footprint import FootprintEngine
from aitos.intelligence.footprint_signals import FootprintSignalEngine
from aitos.intelligence.funding import funding_rate_score
from aitos.intelligence.liquidity import (
    absorption_proxy_score,
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
from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.intelligence.orderflow_liquidity_interaction import (
    FlowLiquidityInteractionEngine,
)
from aitos.intelligence.rl_policy import NeutralRLScorer, RLPolicyScorer
from aitos.logging_setup import get_logger
from aitos.models.market import OpenInterest
from aitos.models.trade import Opportunity, TradeSide

logger = get_logger("aitos.intelligence.scanner")
TOPIC_SCAN_COMPLETE = "market.opportunity_scanned"
DEFAULT_WEIGHTS: Dict[str, float] = {
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
}
REGIME_FIT_SCORE = {"trending": 9.0, "ranging": 4.0, "volatile": 3.0, "unknown": 5.0}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    direction: TradeSide
    composite_score: float
    component_scores: Dict[str, float]
    rationale: List[str]
    entry_price: float
    atr: float
    regime: str
    scanned_at: str = field(default_factory=_utc_now_iso)


def _volatility_fitness(
    atr_percentile: float, sweet_spot: float = 60.0, tolerance: float = 6.0
) -> float:
    return round(
        max(0.0, min(10.0, 10.0 - abs(atr_percentile - sweet_spot) / tolerance)), 2
    )


def determine_direction(
    structure_direction: str, cvd_score: float
) -> Optional[TradeSide]:
    if structure_direction == "bullish_bos" and cvd_score >= 5.0:
        return TradeSide.LONG
    if structure_direction == "bearish_bos" and cvd_score <= 5.0:
        return TradeSide.SHORT
    if structure_direction == "none":
        if cvd_score >= 6.5:
            return TradeSide.LONG
        if cvd_score <= 3.5:
            return TradeSide.SHORT
    return None


class OpportunityScanner(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        exchange: ExchangeAdapter,
        symbols: List[str],
        timeframe: str = "15m",
        reference_symbol: str = "BTCUSDT",
        rl_scorer: Optional[RLPolicyScorer] = None,
        weights: Optional[Dict[str, float]] = None,
        min_score_threshold: float = 60.0,
        top_n: int = 5,
        kline_lookback: int = 100,
        trade_lookback: int = 500,
        footprint_tick_sizes: Optional[Dict[str, float]] = None,
        live_state_stale_seconds: float = 5.0,
        amt_value_area_pct: float = 0.70,
        amt_ib_minutes: int = 60,
    ) -> None:
        self._event_bus = event_bus
        self._exchange = exchange
        self._symbols = symbols
        self._timeframe = timeframe
        self._reference_symbol = reference_symbol
        self._rl_scorer = rl_scorer or NeutralRLScorer()
        self._weights = weights or DEFAULT_WEIGHTS
        self._min_score_threshold, self._top_n = min_score_threshold, top_n
        self._kline_lookback, self._trade_lookback = kline_lookback, trade_lookback
        self._footprint_tick_sizes = footprint_tick_sizes or {}
        self._live_state_stale_seconds = max(0.5, live_state_stale_seconds)
        self._amt_value_area_pct = amt_value_area_pct
        self._amt_ib_minutes = amt_ib_minutes
        self._initialized = False
        self._last_oi = {}
        self._last_amt_profile = {}
        self._last_amt_context = {}
        self._liquidity_trackers = {}
        self._footprint_engines = {}
        self._amt_engines = {}
        self._footprint_signal_engine = FootprintSignalEngine()
        self._interaction_engine = FlowLiquidityInteractionEngine()
        self._live_cache = LiveScannerCache(
            event_bus, symbols, max_trades=max(5000, trade_lookback)
        )
        self._last_scan_at = None
        self._last_candidate_count = 0

    @property
    def module_id(self) -> str:
        return "opportunity-scanner"

    @property
    def version(self) -> str:
        return "1.9.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        await self._exchange.connect()
        await self._live_cache.initialize()
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
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        await self._live_cache.shutdown()
        await self._exchange.close()

    async def emit_events(self) -> AsyncIterator[Event]:
        return

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    def _footprint_tick_size(self, symbol: str) -> Optional[float]:
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
    ) -> Optional[AMTContext]:
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
        self, symbol: str, reference_klines: Optional[list] = None
    ) -> Optional[ScanCandidate]:
        self._require_initialized()
        klines = await self._exchange.fetch_klines(
            symbol, self._timeframe, limit=self._kline_lookback
        )
        if len(klines) < 20:
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
        structure_direction, structure_strength = indicators.detect_structure_break(
            klines
        )
        flow_features = (
            OrderFlowEngine(max_trades=max(100, self._trade_lookback)).ingest_many(
                trades
            )
            if trades
            else None
        )
        candle_cvd = indicators.cvd_trend_score(klines)
        flow_score = flow_features.bias_score if flow_features else candle_cvd
        direction = determine_direction(structure_direction, flow_score)
        self._last_oi[symbol] = oi_current

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
                    "direction": direction.value if direction else "none",
                    "scanner_threshold": self._min_score_threshold,
                }
            },
        )
        if direction is None:
            return None
        trend_score = min(10.0, indicators.adx(klines) / 10.0)
        volatility_score = _volatility_fitness(vol_percentile)
        regime_score = REGIME_FIT_SCORE.get(regime, 5.0)
        lead_lag = (
            indicators.lead_lag_score(klines, reference_klines)
            if reference_klines and symbol != self._reference_symbol
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
        if tick_size is not None and trades:
            footprint = self._footprint_engines.setdefault(
                symbol, FootprintEngine(tick_size)
            ).build(trades)
            footprint_signals = self._footprint_signal_engine.evaluate(footprint)
            interaction = self._interaction_engine.evaluate(
                footprint_signals, liquidity_events
            )
            interaction_score = (
                5.0
                if interaction.direction == "neutral"
                else (
                    5.0 + interaction.score * 0.5
                    if interaction.direction == direction.value
                    else max(0.0, 5.0 - interaction.score * 0.5)
                )
            )
            interaction_score = round(min(10.0, max(0.0, interaction_score)), 2)
            interaction_rationale = f"footprint={footprint_signals.bias}, interaction={interaction.kind}, interaction_score={interaction_score:.1f}, tick_size={tick_size:g}"
        else:
            interaction_score, interaction_rationale = (
                5.0,
                "footprint=not_configured; interaction=neutral",
            )
        rl_context = {
            "regime": regime,
            "direction": direction.value,
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
        }
        weight_total = sum(self._weights.get(k, 0.0) for k in component_scores)
        composite = (
            sum(
                component_scores[k] * self._weights.get(k, 0.0)
                for k in component_scores
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
            f"executed_trades={len(trades)}, candle_cvd={candle_cvd:.1f}, live_auction={live_auction:.1f}, candle_auction={candle_auction:.1f}, amt_score={amt_score:.1f}, regime={regime}, structure={structure_direction}, direction={direction.value}, market_source={market_source}"
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
                f"regime={regime}",
            ]
        )
        return ScanCandidate(
            symbol=symbol,
            direction=direction,
            composite_score=round(composite, 2),
            component_scores=component_scores,
            rationale=rationale,
            entry_price=klines[-1].close,
            atr=atr,
            regime=regime,
        )

    async def scan_all(self) -> List[ScanCandidate]:
        self._require_initialized()
        reference_klines = None
        if self._reference_symbol:
            try:
                reference_klines = await self._exchange.fetch_klines(
                    self._reference_symbol, self._timeframe, limit=self._kline_lookback
                )
            except Exception as exc:
                logger.error("failed to fetch reference symbol klines: %s", exc)
        candidates = []
        for symbol in self._symbols:
            try:
                candidate = await self.scan_symbol(symbol, reference_klines)
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:
                logger.error(
                    "scan failed for symbol",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )
        self._last_scan_at, self._last_candidate_count = _utc_now_iso(), len(candidates)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_SCAN_COMPLETE,
                payload={
                    "symbols_scanned": len(self._symbols),
                    "candidates_found": len(candidates),
                },
                source_module=self.module_id,
            )
        )
        return candidates

    async def rank(
        self, candidates: List[ScanCandidate], top_n: Optional[int] = None
    ) -> List[ScanCandidate]:
        n = top_n if top_n is not None else self._top_n
        return sorted(
            [c for c in candidates if c.composite_score >= self._min_score_threshold],
            key=lambda c: c.composite_score,
            reverse=True,
        )[:n]

    async def decide_with_kernel(self, candidate: ScanCandidate, kernel: Any) -> Any:
        from aitos.kernel.ai_kernel import DecisionContext

        return await kernel.request_decision(
            DecisionContext(
                symbol=candidate.symbol,
                context={
                    "direction": candidate.direction.value,
                    "component_scores": candidate.component_scores,
                    "regime": candidate.regime,
                    "entry_price": candidate.entry_price,
                },
            )
        )

    def to_opportunity(
        self,
        candidate: ScanCandidate,
        risk_reward_multiples: Tuple[float, ...] = (1.0, 2.0, 3.0),
        atr_stop_multiplier: float = 1.5,
        strategy_id: str = "opportunity-scanner",
        is_production: bool = False,
        approved_by: Optional[str] = None,
    ) -> Opportunity:
        entry = candidate.entry_price
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
            agent_consensus=dict(candidate.component_scores),
            is_production=is_production,
            approved_by=approved_by,
            trailing_sl_enabled=True,
            regime=candidate.regime,
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "OpportunityScanner.initialize() must be called first"
            )
