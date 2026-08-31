"""Position Manager — orchestration of market-path, exit and hedge intelligence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.exit_intelligence import ExitAction, ExitDecision, ExitIntelligenceEngine
from aitos.intelligence.hedge_intelligence import HedgeAction, HedgeDecision, HedgeIntelligenceEngine
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
            "exit_decision": self.exit_decision.to_dict() if self.exit_decision else None,
            "hedge_decision": self.hedge_decision.to_dict() if self.hedge_decision else None,
            "thesis_health": self.thesis_eval.health.value if self.thesis_eval else None,
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
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._mse = market_state_engine or MarketStateEngine()
        self._mpp = path_planner or MarketPathPlanner()
        self._sre = structural_risk_engine or StructuralRiskEngine()
        self._eie = exit_intelligence_engine or ExitIntelligenceEngine()
        self._thesis_engine = thesis_engine or TradeThesisEngine()
        self._cfg = dict(config or {})
        self._theses: dict[str, TradeThesis] = {}
        self._hedge_opened_at: dict[str, datetime] = {}
        hedge_cfg = self._cfg.get("hedge", {})
        self._hie = hedge_intelligence_engine or HedgeIntelligenceEngine(hedge_cfg)
        self._allow_stop_tighten = bool(self._cfg.get("allow_stop_tighten", True))

    def register_thesis(self, thesis: TradeThesis) -> None:
        self._theses[thesis.trade_id] = thesis

    def register_hedge(self, trade_id: str, opened_at: datetime | None = None) -> None:
        """Mark a hedge as externally opened so subsequent evaluations can manage it."""
        self._hedge_opened_at[trade_id] = opened_at or datetime.now(timezone.utc)

    def clear_hedge(self, trade_id: str) -> None:
        self._hedge_opened_at.pop(trade_id, None)

    def clear_trade(self, trade_id: str, symbol: str | None = None) -> None:
        self._theses.pop(trade_id, None)
        self._hedge_opened_at.pop(trade_id, None)
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
        hedge_active: bool | None = None,
    ) -> PositionAction:
        ts = timestamp or datetime.now(timezone.utc)
        side = trade.side.value
        market_state = self._mse.compute(
            symbol=trade.symbol,
            mid_price=current_price,
            order_flow=order_flow,
            trend_strength=trend_strength,
            atr_pct=(atr / current_price * 100.0) if atr and current_price > 0 else None,
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
            upside = tuple(d.price for d in path_plan.upside[:3])
            downside = tuple(d.price for d in path_plan.downside[:3])
            expected = upside if side == "LONG" else downside
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
        thesis_eval = self._thesis_engine.evaluate(thesis, market_state, current_price=current_price)
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

        active = hedge_active if hedge_active is not None else trade.trade_id in self._hedge_opened_at
        hedge_decision = self._hie.evaluate(
            symbol=trade.symbol,
            primary_side=side,
            market_state=market_state,
            thesis_eval=thesis_eval,
            exit_action=exit_decision.action,
            current_price=current_price,
            primary_entry_price=trade.entry_price,
            primary_r_distance=trade.r_distance,
            hedge_active=active,
            hedge_opened_at=self._hedge_opened_at.get(trade.trade_id),
            timestamp=ts,
        )
        if hedge_decision.action == HedgeAction.OPEN:
            self._hedge_opened_at.setdefault(trade.trade_id, ts)
        elif hedge_decision.action == HedgeAction.CLOSE:
            self._hedge_opened_at.pop(trade.trade_id, None)

        new_stop: float | None = None
        if self._allow_stop_tighten and exit_decision.action == ExitAction.MANAGE:
            if trade.side == TradeSide.LONG and structural_stop.stop_price > trade.sl_price:
                new_stop = structural_stop.stop_price
            elif trade.side == TradeSide.SHORT and structural_stop.stop_price < trade.sl_price:
                new_stop = structural_stop.stop_price

        reason_codes = [r.code for r in exit_decision.reasons[:5]]
        hedge_reason = ",".join(hedge_decision.reason_codes[:2])
        reason = (
            f"EIE:{exit_decision.action.value} score={exit_decision.exit_score:.2f} "
            f"ere={exit_decision.expected_remaining_edge:.4f} thesis={thesis_eval.health.value} "
            f"[{', '.join(reason_codes)}] HEDGE:{hedge_decision.action.value} "
            f"score={hedge_decision.hedge_score:.2f} [{hedge_reason}]"
        )
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
            notes=exit_decision.notes + hedge_decision.notes,
        )
