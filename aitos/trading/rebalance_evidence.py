"""Translate live PositionManager/Trade Thesis state into rebalancer evidence.

The caller must supply a calibrated estimator that maps the PositionManager's
heuristic exit edge into the same percentage unit as candidate expected edge.
No heuristic score is silently treated as a return percentage.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.capital_rebalancing import IncumbentPosition
from aitos.models.trade import Trade


@dataclass(frozen=True)
class IncumbentIntelligenceSnapshot:
    incumbent: IncumbentPosition
    thesis_consistency: float
    journey_health: float
    path_adherence: float
    stagnation_observations: int
    as_of: str


class PositionManagerEvidenceAdapter:
    def __init__(
        self,
        *,
        position_manager: Any,
        market_context_provider: Any,
        remaining_edge_estimator: Callable[[Any, Trade], float],
        exit_fee_bps_provider: Callable[[Trade], float],
        exit_slippage_bps_provider: Callable[[Trade], float],
        stagnation_bar_threshold: int = 35,
    ) -> None:
        if stagnation_bar_threshold < 1:
            raise ValueError("stagnation_bar_threshold must be positive")
        self.position_manager = position_manager
        self.market_context_provider = market_context_provider
        self.remaining_edge_estimator = remaining_edge_estimator
        self.exit_fee_bps_provider = exit_fee_bps_provider
        self.exit_slippage_bps_provider = exit_slippage_bps_provider
        self.stagnation_bar_threshold = stagnation_bar_threshold

    def evaluate(
        self, trade: Trade, current_price: float, **market_features: Any
    ) -> IncumbentIntelligenceSnapshot:
        if current_price <= 0:
            raise ValueError("current price must be positive")
        context = self.market_context_provider.get_context(trade.symbol)
        action = self.position_manager.evaluate(
            trade=trade,
            current_price=current_price,
            **context.as_kwargs(),
            **market_features,
        )
        thesis_eval = getattr(action, "thesis_eval", None)
        journey = getattr(action, "journey", None)
        if thesis_eval is None or journey is None:
            raise RuntimeError(
                "PositionManager did not return thesis and journey evidence"
            )
        health = getattr(thesis_eval.health, "value", str(thesis_eval.health)).lower()
        observations = max(0, int(getattr(trade, "adaptive_exit_observations", 0)))
        stagnation = min(1.0, observations / self.stagnation_bar_threshold)
        # Hard invalidation is represented as maximal stagnation pressure;
        # degraded thesis health remains a distinct explicit field.
        if health == "invalidated":
            stagnation = 1.0
        remaining_edge_pct = float(self.remaining_edge_estimator(action, trade))
        entry_time = datetime.fromisoformat(trade.entry_time.replace("Z", "+00:00"))
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        incumbent = IncumbentPosition(
            symbol=trade.symbol,
            side=trade.side.value,
            remaining_net_edge_pct=remaining_edge_pct,
            hold_seconds=max(
                0.0, (datetime.now(timezone.utc) - entry_time).total_seconds()
            ),
            stagnation_score=stagnation,
            thesis_health=health,
            exit_fee_bps=float(self.exit_fee_bps_provider(trade)),
            exit_slippage_bps=float(self.exit_slippage_bps_provider(trade)),
        )
        return IncumbentIntelligenceSnapshot(
            incumbent=incumbent,
            thesis_consistency=float(thesis_eval.consistency_score),
            journey_health=float(journey.health_score),
            path_adherence=float(journey.path_adherence),
            stagnation_observations=observations,
            as_of=datetime.now(timezone.utc).isoformat(),
        )
