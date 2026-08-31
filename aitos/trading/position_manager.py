"""Position Manager — Phase E of the Market-Path / Exit-Intelligence architecture.

Orchestrates:

    MarketState → PathPlan → StructuralStop → ExitDecision

and turns the decision into concrete lifecycle actions:

* HOLD   — do nothing (let winners run)
* MANAGE — optional partial reduce + optional structural-stop tighten
* EXIT   — full close with explainable reason

This module is intentionally side-effect light: it returns an action plan.
TradeLifecycle (or a caller) is responsible for executing the plan so that
existing emergency hard-SL / exchange-side paths stay authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.exit_intelligence import (
    ExitAction,
    ExitDecision,
    ExitIntelligenceEngine,
)
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state import MarketState, MarketStateEngine
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.intelligence.path_planner import MarketPathPlanner, PathPlan
from aitos.intelligence.structural_risk import StructuralRiskEngine, StructuralStop
from aitos.logging_setup import get_logger
from aitos.models.trade import Trade, TradeSide

logger = get_logger("aitos.trading.position_manager")

TOPIC_EXIT_DECISION = "decision.exit"
TOPIC_PATH_PLAN = "decision.path_plan"
TOPIC_MARKET_STATE = "decision.market_state"
TOPIC_STRUCTURAL_STOP = "decision.structural_stop"


@dataclass(frozen=True)
class PositionAction:
    """Concrete instruction returned to TradeLifecycle."""

    action: ExitAction
    reason: str
    reduce_fraction: float = 0.0  # 0–1 when MANAGE
    new_stop_price: float | None = None  # tighten toward structural stop
    exit_decision: ExitDecision | None = None
    path_plan: PathPlan | None = None
    structural_stop: StructuralStop | None = None
    market_state: MarketState | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "reduce_fraction": self.reduce_fraction,
            "new_stop_price": self.new_stop_price,
            "exit_decision": self.exit_decision.to_dict() if self.exit_decision else None,
            "notes": list(self.notes),
        }


class PositionManager:
    """Coordinates the four intelligence engines for an open position."""

    def __init__(
        self,
        market_state_engine: MarketStateEngine | None = None,
        path_planner: MarketPathPlanner | None = None,
        structural_risk_engine: StructuralRiskEngine | None = None,
        exit_intelligence_engine: ExitIntelligenceEngine | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._mse = market_state_engine or MarketStateEngine()
        self._mpp = path_planner or MarketPathPlanner()
        self._sre = structural_risk_engine or StructuralRiskEngine()
        self._eie = exit_intelligence_engine or ExitIntelligenceEngine()
        self._cfg = dict(config or {})
        # When True, MANAGE may tighten SL toward structural stop
        self._allow_stop_tighten = bool(self._cfg.get("allow_stop_tighten", True))

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
        """Run the full intelligence stack and return a PositionAction."""
        ts = timestamp or datetime.now(timezone.utc)
        side = trade.side.value

        # 1. Market State
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

        # 2. Path Plan
        path_plan = self._mpp.plan(
            market_state=market_state,
            volume_profile=volume_profile,
            liquidity_events=liquidity_events,
            prior_highs=prior_highs,
            prior_lows=prior_lows,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
        )

        # 3. Structural Stop (thesis invalidation)
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

        # 4. Exit Intelligence
        exit_decision = self._eie.evaluate(
            symbol=trade.symbol,
            side=side,
            entry_price=trade.entry_price,
            current_price=current_price,
            market_state=market_state,
            path_plan=path_plan,
            structural_stop=structural_stop,
            timestamp=ts,
        )

        # 5. Map to PositionAction
        new_stop: float | None = None
        if (
            self._allow_stop_tighten
            and exit_decision.action == ExitAction.MANAGE
            and structural_stop is not None
        ):
            # Only tighten (never loosen) relative to current SL
            if trade.side == TradeSide.LONG:
                if structural_stop.stop_price > trade.sl_price:
                    new_stop = structural_stop.stop_price
            else:
                if structural_stop.stop_price < trade.sl_price:
                    new_stop = structural_stop.stop_price

        reason_codes = [r.code for r in exit_decision.reasons[:5]]
        reason = (
            f"EIE:{exit_decision.action.value}"
            f" score={exit_decision.exit_score:.2f}"
            f" ere={exit_decision.expected_remaining_edge:.4f}"
            f" [{', '.join(reason_codes)}]"
        )

        action = PositionAction(
            action=exit_decision.action,
            reason=reason,
            reduce_fraction=exit_decision.suggested_reduce_fraction,
            new_stop_price=new_stop,
            exit_decision=exit_decision,
            path_plan=path_plan,
            structural_stop=structural_stop,
            market_state=market_state,
            notes=exit_decision.notes,
        )
        logger.debug(
            "PositionAction",
            extra={
                "aitos_extra": {
                    "trade_id": trade.trade_id,
                    "action": action.action.value,
                    "score": exit_decision.exit_score,
                }
            },
        )
        return action
