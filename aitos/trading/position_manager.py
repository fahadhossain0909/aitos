"""Position Manager — Market State, Path, Exit and Hedge orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.exit_intelligence import (
    ExitAction,
    ExitDecision,
    ExitIntelligenceEngine,
)
from aitos.intelligence.hedge_intelligence import HedgeDecision, HedgeIntelligenceEngine
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state import MarketState, MarketStateEngine
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.intelligence.path_planner import MarketPathPlanner, PathPlan
from aitos.intelligence.structural_risk import StructuralRiskEngine, StructuralStop
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
    exit_decision: ExitDecision | None = None
    hedge_decision: HedgeDecision | None = None
    path_plan: PathPlan | None = None
    structural_stop: StructuralStop | None = None
    market_state: MarketState | None = None
    thesis: TradeThesis | None = None
    thesis_eval: ThesisEvaluation | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "reduce_fraction": self.reduce_fraction,
            "new_stop_price": self.new_stop_price,
            "exit_decision": (
                self.exit_decision.to_dict() if self.exit_decision else None
            ),
            "hedge_decision": (
                self.hedge_decision.to_dict() if self.hedge_decision else None
            ),
            "thesis_health": (
                self.thesis_eval.health.value if self.thesis_eval else None
            ),
            "notes": list(self.notes),
        }


class PositionManager:
    def __init__(
        self,
        market_state_engine=None,
        path_planner=None,
        structural_risk_engine=None,
        exit_intelligence_engine=None,
        thesis_engine=None,
        hedge_intelligence_engine=None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._mse = market_state_engine or MarketStateEngine()
        self._mpp = path_planner or MarketPathPlanner()
        self._sre = structural_risk_engine or StructuralRiskEngine()
        self._eie = exit_intelligence_engine or ExitIntelligenceEngine()
        self._thesis_engine = thesis_engine or TradeThesisEngine()
        cfg = dict(config or {})
        self._cfg = cfg
        self._hedge_engine = hedge_intelligence_engine or HedgeIntelligenceEngine(
            open_threshold=float(cfg.get("hedge_open_threshold", 0.68)),
            close_threshold=float(cfg.get("hedge_close_threshold", 0.56)),
            max_ratio=float(cfg.get("hedge_max_ratio", 0.50)),
            min_ratio=float(cfg.get("hedge_min_ratio", 0.20)),
            min_benefit_cost_ratio=float(
                cfg.get("hedge_min_benefit_cost_ratio", 2.0)
            ),
            estimated_roundtrip_cost_rate=float(
                cfg.get("hedge_estimated_roundtrip_cost_rate", 0.0012)
            ),
        )
        self._hedge_enabled = bool(cfg.get("hedge_enabled", True))
        self._theses: dict[str, TradeThesis] = {}
        self._allow_stop_tighten = bool(cfg.get("allow_stop_tighten", True))

    def register_thesis(self, thesis: TradeThesis) -> None:
        self._theses[thesis.trade_id] = thesis

    def clear_trade(self, trade_id: str, symbol: str | None = None) -> None:
        self._theses.pop(trade_id, None)
        self._hedge_engine.reset(trade_id)
        if symbol:
            self._eie.reset_symbol(symbol)

    def evaluate(
        self,
        *,
        trade: Trade,
        current_price: float,
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
        timestamp: datetime | None = None,
    ) -> PositionAction:
        ts = timestamp or datetime.now(timezone.utc)
        side = trade.side.value
        market_state = self._mse.compute(
            symbol=trade.symbol,
            mid_price=current_price,
            order_flow=order_flow,
            trend_strength=trend_strength,
            atr_pct=(
                (atr / current_price * 100.0) if atr and current_price > 0 else None
            ),
            volume_profile_poc=volume_profile.poc if volume_profile else None,
            value_area_high=volume_profile.vah if volume_profile else None,
            value_area_low=volume_profile.val if volume_profile else None,
            structure_bias_hint=None,
            timestamp=ts,
            extra_features=extra_features,
        )
        path_plan = self._mpp.plan(
            market_state=market_state,
            volume_profile=volume_profile,
            liquidity_events=liquidity_events,
            prior_highs=prior_highs,
            prior_lows=prior_lows,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
        )
        structural_stop = self._sre.compute(
            symbol=trade.symbol,
            side=side,
            entry_price=trade.entry_price,
            market_state=market_state,
            volume_profile=volume_profile,
            swing_lows=swing_lows,
            swing_highs=swing_highs,
            structure_break_level=structure_break_level,
            liquidity_events=liquidity_events,
            atr=atr,
            timestamp=ts,
        )
        thesis = self._theses.get(trade.trade_id)
        if thesis is None:
            expected = tuple(
                d.price
                for d in (path_plan.upside if side == "LONG" else path_plan.downside)[:3]
            )
            thesis = self._thesis_engine.build_from_entry(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                side=side,
                entry_price=trade.entry_price,
                market_state=market_state,
                structural_invalidation_price=structural_stop.stop_price,
                expected_path_prices=expected,
                strategy_id=trade.strategy_id,
                rationale=trade.explanation or "",
                timestamp=ts,
            )
            self._theses[trade.trade_id] = thesis
        thesis_eval = self._thesis_engine.evaluate(
            thesis, market_state, current_price=current_price
        )
        exit_decision = self._eie.evaluate(
            symbol=trade.symbol,
            side=side,
            entry_price=trade.entry_price,
            current_price=current_price,
            market_state=market_state,
            path_plan=path_plan,
            structural_stop=structural_stop,
            thesis=thesis,
            thesis_eval=thesis_eval,
            timestamp=ts,
        )
        hedge_decision = (
            self._hedge_engine.evaluate(
                trade=trade,
                market_state=market_state,
                current_price=current_price,
                timestamp=ts,
                atr=atr,
            )
            if self._hedge_enabled and exit_decision.action != ExitAction.EXIT
            else None
        )
        new_stop = None
        if self._allow_stop_tighten and exit_decision.action == ExitAction.MANAGE:
            if (
                trade.side == TradeSide.LONG
                and structural_stop.stop_price > trade.sl_price
            ) or (
                trade.side == TradeSide.SHORT
                and structural_stop.stop_price < trade.sl_price
            ):
                new_stop = structural_stop.stop_price
        reason_codes = [r.code for r in exit_decision.reasons[:5]]
        hedge_text = f" hedge={hedge_decision.action}" if hedge_decision else ""
        reason = f"EIE:{exit_decision.action.value} score={exit_decision.exit_score:.2f} ere={exit_decision.expected_remaining_edge:.4f} thesis={thesis_eval.health.value}{hedge_text} [{', '.join(reason_codes)}]"
        return PositionAction(
            action=exit_decision.action,
            reason=reason,
            reduce_fraction=exit_decision.suggested_reduce_fraction,
            new_stop_price=new_stop,
            exit_decision=exit_decision,
            hedge_decision=hedge_decision,
            path_plan=path_plan,
            structural_stop=structural_stop,
            market_state=market_state,
            thesis=thesis,
            thesis_eval=thesis_eval,
            notes=exit_decision.notes,
        )
