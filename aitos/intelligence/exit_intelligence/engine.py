"""Exit Intelligence Engine — continuation vs invalidation.

Core rules (from the architecture design):

* Momentum slowdown alone is NEVER sufficient for EXIT.
* Multiple independent evidences of thesis breakdown → EXIT.
* Expected Remaining Edge (ERE) > 0 → prefer HOLD.
* ERE ≈ 0 → MANAGE.
* ERE < 0 and exit_score high → EXIT.

All scoring is deterministic and fully auditable via the reasons list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from aitos.intelligence.exit_intelligence.models import (
    ExitAction,
    ExitDecision,
    ExitReason,
)
from aitos.intelligence.market_state.models import (
    MarketState,
    MomentumState,
    OrderFlowBias,
    StructureBias,
)
from aitos.intelligence.path_planner.models import PathPlan
from aitos.intelligence.structural_risk.models import StructuralStop
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.exit_intelligence")

# Score thresholds
EXIT_SCORE_EXIT = 0.65
EXIT_SCORE_MANAGE = 0.40
ERE_HOLD_THRESHOLD = 0.15  # relative edge units
ERE_EXIT_THRESHOLD = -0.05


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class ExitIntelligenceEngine:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(config or {})

    def evaluate(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        market_state: MarketState,
        path_plan: PathPlan | None = None,
        structural_stop: StructuralStop | None = None,
        transaction_cost_pct: float = 0.0004,
        timestamp: datetime | None = None,
    ) -> ExitDecision:
        """Produce a HOLD / MANAGE / EXIT decision with full reason audit trail."""
        side = side.upper()
        ts = timestamp or market_state.timestamp or datetime.now(timezone.utc)
        reasons: list[ExitReason] = []
        features: dict[str, float] = {
            "entry_price": entry_price,
            "current_price": current_price,
            "unrealized_pct": (current_price - entry_price)
            / entry_price
            * (1.0 if side == "LONG" else -1.0),
        }
        notes: list[str] = []

        # ---- 1. Thesis / structure health ------------------------------------
        reasons.extend(self._structure_reasons(side, market_state))
        reasons.extend(self._order_flow_reasons(side, market_state))
        reasons.extend(self._momentum_reasons(market_state))

        # ---- 2. Path / destination health ------------------------------------
        ere, path_reasons = self._path_and_ere(
            side, current_price, path_plan, transaction_cost_pct
        )
        reasons.extend(path_reasons)
        features["expected_remaining_edge"] = ere

        # ---- 3. Proximity to structural stop ---------------------------------
        if structural_stop is not None:
            stop_dist_pct = abs(current_price - structural_stop.stop_price) / current_price
            features["dist_to_structural_stop_pct"] = stop_dist_pct
            if stop_dist_pct < 0.003:
                reasons.append(
                    ExitReason(
                        "near_structural_stop",
                        f"Price within {stop_dist_pct:.2%} of structural invalidation",
                        +0.18,
                    )
                )

        # ---- 4. Reversal risk from MarketState -------------------------------
        rr = market_state.reversal_risk
        features["reversal_risk"] = rr
        if rr >= 0.55:
            reasons.append(
                ExitReason(
                    "elevated_reversal_risk",
                    f"MarketState reversal_risk={rr:.2f}",
                    +0.12 + (rr - 0.55) * 0.3,
                )
            )
        elif rr <= 0.25:
            reasons.append(
                ExitReason(
                    "low_reversal_risk",
                    f"MarketState reversal_risk={rr:.2f}",
                    -0.08,
                )
            )

        # ---- Aggregate exit score -------------------------------------------
        raw_score = sum(r.weight for r in reasons)
        # Map roughly from [-1.5, +1.5] → [0, 1]
        exit_score = _clamp01(0.5 + raw_score / 2.0)
        features["exit_score_raw"] = raw_score
        features["exit_score"] = exit_score

        # ---- Decision policy ------------------------------------------------
        action, reduce_frac = self._decide(exit_score, ere, reasons)
        notes.append(f"action={action.value} score={exit_score:.3f} ere={ere:.4f}")

        decision = ExitDecision(
            symbol=symbol,
            side=side,
            action=action,
            exit_score=round(exit_score, 4),
            expected_remaining_edge=round(ere, 6),
            reasons=tuple(reasons),
            suggested_reduce_fraction=reduce_frac,
            as_of=ts,
            features=features,
            notes=tuple(notes),
        )
        logger.debug(
            "ExitDecision",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "action": action.value,
                    "score": exit_score,
                    "ere": ere,
                }
            },
        )
        return decision

    # ------------------------------------------------------------------
    # Reason generators
    # ------------------------------------------------------------------

    def _structure_reasons(
        self, side: str, state: MarketState
    ) -> list[ExitReason]:
        reasons: list[ExitReason] = []
        if state.structure == StructureBias.BROKEN:
            reasons.append(
                ExitReason("structure_broken", "Market structure marked BROKEN", +0.30)
            )
        elif side == "LONG" and state.structure == StructureBias.BEARISH:
            reasons.append(
                ExitReason(
                    "structure_against",
                    "LONG but structure is BEARISH",
                    +0.22,
                )
            )
        elif side == "SHORT" and state.structure == StructureBias.BULLISH:
            reasons.append(
                ExitReason(
                    "structure_against",
                    "SHORT but structure is BULLISH",
                    +0.22,
                )
            )
        elif (
            (side == "LONG" and state.structure == StructureBias.BULLISH)
            or (side == "SHORT" and state.structure == StructureBias.BEARISH)
        ):
            reasons.append(
                ExitReason(
                    "structure_aligned",
                    f"Structure {state.structure.value} supports position",
                    -0.12,
                )
            )
        return reasons

    def _order_flow_reasons(
        self, side: str, state: MarketState
    ) -> list[ExitReason]:
        reasons: list[ExitReason] = []
        of = state.order_flow_bias
        if side == "LONG" and of == OrderFlowBias.SELLER_DOMINANT:
            reasons.append(
                ExitReason(
                    "of_reversal",
                    "Order-flow flipped to SELLER_DOMINANT against LONG",
                    +0.20,
                )
            )
        elif side == "SHORT" and of == OrderFlowBias.BUYER_DOMINANT:
            reasons.append(
                ExitReason(
                    "of_reversal",
                    "Order-flow flipped to BUYER_DOMINANT against SHORT",
                    +0.20,
                )
            )
        elif (
            (side == "LONG" and of == OrderFlowBias.BUYER_DOMINANT)
            or (side == "SHORT" and of == OrderFlowBias.SELLER_DOMINANT)
        ):
            reasons.append(
                ExitReason(
                    "of_supportive",
                    f"Order-flow {of.value} supports position",
                    -0.10,
                )
            )
        return reasons

    def _momentum_reasons(self, state: MarketState) -> list[ExitReason]:
        """Momentum alone never forces EXIT — only contributes mild pressure."""
        reasons: list[ExitReason] = []
        if state.momentum == MomentumState.EXHAUSTED:
            reasons.append(
                ExitReason(
                    "momentum_exhausted",
                    "Momentum EXHAUSTED (mild; needs confirmation)",
                    +0.08,
                )
            )
        elif state.momentum == MomentumState.WEAK:
            reasons.append(
                ExitReason(
                    "momentum_weak",
                    "Momentum WEAK (mild; needs confirmation)",
                    +0.05,
                )
            )
        elif state.momentum == MomentumState.STRONG:
            reasons.append(
                ExitReason("momentum_strong", "Momentum still STRONG", -0.08)
            )
        return reasons

    def _path_and_ere(
        self,
        side: str,
        current_price: float,
        plan: PathPlan | None,
        cost_pct: float,
    ) -> tuple[float, list[ExitReason]]:
        reasons: list[ExitReason] = []
        if plan is None or current_price <= 0:
            return 0.0, reasons

        # Expected upside / downside from remaining destinations
        if side == "LONG":
            upside_dests = plan.upside
            downside_dests = plan.downside
        else:
            upside_dests = plan.downside  # “upside” for a short = lower prices
            downside_dests = plan.upside

        expected_gain = 0.0
        total_up_prob = 0.0
        for d in upside_dests:
            rel = abs(d.price - current_price) / current_price
            expected_gain += d.probability * rel
            total_up_prob += d.probability

        expected_loss = 0.0
        total_down_prob = 0.0
        for d in downside_dests:
            rel = abs(d.price - current_price) / current_price
            expected_loss += d.probability * rel
            total_down_prob += d.probability

        # Normalise roughly if probabilities are not a partition
        if total_up_prob + total_down_prob > 1e-9:
            scale = 1.0  # keep absolute; they are already 0-1 scores
        else:
            scale = 1.0

        ere = (expected_gain - expected_loss) * scale - cost_pct

        if total_up_prob >= 0.55:
            reasons.append(
                ExitReason(
                    "path_upside_alive",
                    f"Upside path probability mass ≈ {total_up_prob:.2f}",
                    -0.12,
                )
            )
        elif total_up_prob <= 0.25 and total_down_prob >= 0.40:
            reasons.append(
                ExitReason(
                    "path_upside_collapsed",
                    f"Upside mass {total_up_prob:.2f}, downside {total_down_prob:.2f}",
                    +0.18,
                )
            )

        if ere > ERE_HOLD_THRESHOLD:
            reasons.append(
                ExitReason(
                    "positive_ere",
                    f"Expected remaining edge={ere:.4f}",
                    -0.10,
                )
            )
        elif ere < ERE_EXIT_THRESHOLD:
            reasons.append(
                ExitReason(
                    "negative_ere",
                    f"Expected remaining edge={ere:.4f}",
                    +0.15,
                )
            )

        return ere, reasons

    def _decide(
        self,
        exit_score: float,
        ere: float,
        reasons: list[ExitReason],
    ) -> tuple[ExitAction, float]:
        # Hard EXIT only when score is high AND edge is non-positive
        if exit_score >= EXIT_SCORE_EXIT and ere <= ERE_HOLD_THRESHOLD:
            return ExitAction.EXIT, 1.0

        if exit_score >= EXIT_SCORE_MANAGE or (
            ere <= 0.0 and exit_score >= 0.30
        ):
            # Partial reduce suggestion scales with score
            frac = _clamp01((exit_score - 0.25) / 0.5)
            return ExitAction.MANAGE, round(max(0.25, min(0.75, frac)), 2)

        return ExitAction.HOLD, 0.0
