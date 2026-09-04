"""Position Manager — Market State, Path, Journey, Exit, Hedge orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.exit_intelligence import ExitAction, ExitDecision, ExitIntelligenceEngine
from aitos.intelligence.hedge_intelligence import HedgeDecision, HedgeIntelligenceEngine
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state import MarketState, MarketStateEngine
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.intelligence.path_planner import MarketPathPlanner, PathPlan
from aitos.intelligence.structural_risk import StructuralRiskEngine, StructuralStop
from aitos.intelligence.trade_journey import TradeJourneyEngine, TradeJourneySnapshot
from aitos.intelligence.trade_thesis import TradeThesis, TradeThesisEngine
from aitos.intelligence.trade_thesis.models import ThesisEvaluation
from aitos.logging_setup import get_logger
from aitos.models.trade import Trade, TradeSide

logger = get_logger("aitos.trading.position_manager")


@dataclass(frozen=True)
class PositionAction:
    action: ExitAction
    reason: str
    reduce_fraction: float = 0.0
    new_stop_price: float | None = None
    spike_tp_price: float | None = None
    exit_decision: ExitDecision | None = None
    hedge_decision: HedgeDecision | None = None
    path_plan: PathPlan | None = None
    structural_stop: StructuralStop | None = None
    market_state: MarketState | None = None
    thesis: TradeThesis | None = None
    thesis_eval: ThesisEvaluation | None = None
    journey: TradeJourneySnapshot | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "reduce_fraction": self.reduce_fraction,
            "new_stop_price": self.new_stop_price,
            "spike_tp_price": self.spike_tp_price,
            "exit_decision": self.exit_decision.to_dict() if self.exit_decision else None,
            "hedge_decision": self.hedge_decision.to_dict() if self.hedge_decision else None,
            "thesis_health": self.thesis_eval.health.value if self.thesis_eval else None,
            "journey": self.journey.to_dict() if self.journey else None,
            "notes": list(self.notes),
        }


class PositionManager:
    def __init__(
        self,
        market_state_engine: MarketStateEngine | None = None,
        path_planner: MarketPathPlanner | None = None,
        structural_risk_engine: StructuralRiskEngine | None = None,
        exit_intelligence_engine: ExitIntelligenceEngine | None = None,
        thesis_engine: TradeThesisEngine | None = None,
        hedge_intelligence_engine: HedgeIntelligenceEngine | None = None,
        trade_journey_engine: TradeJourneyEngine | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._mse = market_state_engine or MarketStateEngine()
        self._mpp = path_planner or MarketPathPlanner()
        self._sre = structural_risk_engine or StructuralRiskEngine()
        self._eie = exit_intelligence_engine or ExitIntelligenceEngine()
        self._thesis_engine = thesis_engine or TradeThesisEngine()
        cfg = dict(config or {})
        self._cfg = cfg
        self._journey_engine = trade_journey_engine or TradeJourneyEngine(
            proving_max_r=float(cfg.get("journey_proving_max_r", 0.50)),
            healthy_threshold=float(cfg.get("journey_healthy_threshold", 0.70)),
            uncertain_threshold=float(cfg.get("journey_uncertain_threshold", 0.48)),
            decay_threshold=float(cfg.get("journey_decay_threshold", 0.34)),
            stale_after_seconds=float(cfg.get("journey_stale_after_seconds", 900.0)),
            reduce_health_threshold=float(cfg.get("journey_reduce_health_threshold", 0.42)),
            max_reduce_fraction=float(cfg.get("journey_max_reduce_fraction", 0.50)),
        )
        self._hedge_engine = hedge_intelligence_engine or HedgeIntelligenceEngine(
            open_threshold=float(cfg.get("hedge_open_threshold", 0.68)),
            close_threshold=float(cfg.get("hedge_close_threshold", 0.56)),
            max_ratio=float(cfg.get("hedge_max_ratio", 0.50)),
            min_ratio=float(cfg.get("hedge_min_ratio", 0.20)),
            min_benefit_cost_ratio=float(cfg.get("hedge_min_benefit_cost_ratio", 2.0)),
            estimated_roundtrip_cost_rate=float(cfg.get("hedge_estimated_roundtrip_cost_rate", 0.0012)),
        )
        self._hedge_enabled = bool(cfg.get("hedge_enabled", True))
        self._theses: dict[str, TradeThesis] = {}
        self._journeys: dict[str, TradeJourneySnapshot] = {}
        self._allow_stop_tighten = bool(cfg.get("allow_stop_tighten", True))
        self._spike_tp_atr_multiple = max(0.0, float(cfg.get("spike_tp_atr_multiple", 10.0)))

    def register_thesis(self, thesis: TradeThesis) -> None:
        self._theses[thesis.trade_id] = thesis

    def clear_trade(self, trade_id: str, symbol: str | None = None) -> None:
        self._theses.pop(trade_id, None)
        self._journeys.pop(trade_id, None)
        self._hedge_engine.reset(trade_id)
        if symbol:
            self._eie.reset_symbol(symbol)

    def _spike_tp(self, *, side: str, current_price: float, atr: float | None) -> float | None:
        if atr is None or atr <= 0 or current_price <= 0 or self._spike_tp_atr_multiple <= 0:
            return None
        distance = atr * self._spike_tp_atr_multiple
        return current_price + distance if side == "LONG" else current_price - distance

    def evaluate(self, *, trade: Trade, current_price: float, order_flow: OrderFlowFeatures | None = None,
                 volume_profile: VolumeProfile | None = None, liquidity_events: Sequence[LiquidityEvent] = (),
                 prior_highs: Sequence[float] = (), prior_lows: Sequence[float] = (),
                 swing_highs: Sequence[float] = (), swing_lows: Sequence[float] = (),
                 structure_break_level: float | None = None, atr: float | None = None,
                 trend_strength: float | None = None, extra_features: Mapping[str, float] | None = None,
                 timestamp: datetime | None = None) -> PositionAction:
        ts = timestamp or datetime.now(timezone.utc)
        side = trade.side.value
        trade.record_excursion(current_price)
        market_state = self._mse.compute(
            symbol=trade.symbol, mid_price=current_price, order_flow=order_flow,
            trend_strength=trend_strength,
            atr_pct=(atr / current_price * 100.0) if atr and current_price > 0 else None,
            volume_profile_poc=volume_profile.poc if volume_profile else None,
            value_area_high=volume_profile.vah if volume_profile else None,
            value_area_low=volume_profile.val if volume_profile else None,
            structure_bias_hint=None, timestamp=ts, extra_features=extra_features,
        )
        path_plan = self._mpp.plan(
            market_state=market_state, volume_profile=volume_profile, liquidity_events=liquidity_events,
            prior_highs=prior_highs, prior_lows=prior_lows, swing_highs=swing_highs, swing_lows=swing_lows,
        )
        structural_stop = self._sre.compute(
            symbol=trade.symbol, side=side, entry_price=trade.entry_price, market_state=market_state,
            volume_profile=volume_profile, swing_lows=swing_lows, swing_highs=swing_highs,
            structure_break_level=structure_break_level, liquidity_events=liquidity_events, atr=atr, timestamp=ts,
        )
        thesis = self._theses.get(trade.trade_id)
        if thesis is None:
            expected = tuple(d.price for d in (path_plan.upside if side == "LONG" else path_plan.downside)[:3])
            thesis = self._thesis_engine.build_from_entry(
                trade_id=trade.trade_id, symbol=trade.symbol, side=side, entry_price=trade.entry_price,
                market_state=market_state, structural_invalidation_price=structural_stop.stop_price,
                expected_path_prices=expected, strategy_id=trade.strategy_id,
                rationale=trade.explanation or "", timestamp=ts,
            )
            self._theses[trade.trade_id] = thesis
        thesis_eval = self._thesis_engine.evaluate(thesis, market_state, current_price=current_price)
        expected_path = tuple(d.price for d in (path_plan.upside if side == "LONG" else path_plan.downside)[:5])
        age_seconds = 0.0
        try:
            entry_dt = datetime.fromisoformat(trade.entry_time.replace("Z", "+00:00"))
            age_seconds = max(0.0, (ts - entry_dt).total_seconds())
        except (TypeError, ValueError):
            pass
        thesis_health_map = {"HEALTHY": 1.0, "STABLE": 0.78, "WEAK": 0.50, "INVALID": 0.10}
        thesis_health = thesis_health_map.get(getattr(thesis_eval.health, "value", str(thesis_eval.health)), 0.50)
        journey = self._journey_engine.evaluate(
            side=side, entry_price=trade.entry_price, current_price=current_price,
            unrealized_r=trade.unrealized_r_multiple(current_price), age_seconds=age_seconds,
            thesis_health=thesis_health,
            momentum=getattr(market_state, "trend_strength", None),
            liquidity=getattr(market_state, "liquidity_score", None),
            structure=getattr(market_state, "structure_score", None),
            expected_path_prices=expected_path,
        )
        self._journeys[trade.trade_id] = journey
        exit_decision = self._eie.evaluate(
            symbol=trade.symbol, side=side, entry_price=trade.entry_price, current_price=current_price,
            market_state=market_state, path_plan=path_plan, structural_stop=structural_stop,
            thesis=thesis, thesis_eval=thesis_eval, timestamp=ts,
        )
        hedge_decision = (
            self._hedge_engine.evaluate(trade=trade, market_state=market_state, current_price=current_price, timestamp=ts, atr=atr)
            if self._hedge_enabled and exit_decision.action != ExitAction.EXIT else None
        )
        effective_action = exit_decision.action
        reduce_fraction = exit_decision.suggested_reduce_fraction
        if effective_action != ExitAction.EXIT:
            if journey.action.value == "EXIT":
                effective_action = ExitAction.EXIT
            elif journey.action.value == "REDUCE":
                effective_action = ExitAction.MANAGE
                reduce_fraction = max(reduce_fraction, min(0.50, self._journey_engine.max_reduce_fraction))
            elif journey.action.value in {"PROTECT", "TRAIL"}:
                effective_action = ExitAction.MANAGE
        new_stop = None
        if self._allow_stop_tighten and effective_action == ExitAction.MANAGE and structural_stop is not None:
            if trade.side == TradeSide.LONG and structural_stop.stop_price > trade.sl_price:
                new_stop = structural_stop.stop_price
            elif trade.side == TradeSide.SHORT and structural_stop.stop_price < trade.sl_price:
                new_stop = structural_stop.stop_price
        spike_tp_price = self._spike_tp(side=side, current_price=current_price, atr=atr)
        reason_codes = [r.code for r in exit_decision.reasons[:5]]
        reason = (
            f"EIE:{exit_decision.action.value} score={exit_decision.exit_score:.2f}"
            f" ere={exit_decision.expected_remaining_edge:.4f} thesis={thesis_eval.health.value}"
            f" journey={journey.state.value}/{journey.action.value} health={journey.health_score:.1f}"
            f" path={journey.path_adherence:.1f}"
            f" hedge={hedge_decision.action if hedge_decision else 'none'}"
            f" [{', '.join(reason_codes + list(journey.reasons))}]"
        )
        notes = tuple(dict.fromkeys((*exit_decision.notes, *journey.reasons)))
        return PositionAction(
            action=effective_action, reason=reason, reduce_fraction=reduce_fraction,
            new_stop_price=new_stop, spike_tp_price=spike_tp_price,
            exit_decision=exit_decision, hedge_decision=hedge_decision,
            path_plan=path_plan, structural_stop=structural_stop, market_state=market_state,
            thesis=thesis, thesis_eval=thesis_eval, journey=journey, notes=notes,
        )
