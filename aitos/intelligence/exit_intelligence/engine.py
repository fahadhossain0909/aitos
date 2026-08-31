"""Exit Intelligence Engine — continuation vs invalidation.

Core rules:
* Momentum slowdown alone is NEVER sufficient for EXIT.
* Structure break / thesis INVALIDATED → EXIT without hysteresis.
* Soft exit pressure requires N consecutive observations (hysteresis).
* ERE is a *heuristic* edge (soft-normalised scores, not calibrated EV).
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

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
from aitos.intelligence.trade_thesis.models import (
    ThesisEvaluation,
    ThesisHealth,
    TradeThesis,
)
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.exit_intelligence")

EXIT_SCORE_EXIT = 0.65
EXIT_SCORE_MANAGE = 0.40
ERE_HOLD_THRESHOLD = 0.15
ERE_EXIT_THRESHOLD = -0.05
DEFAULT_EXIT_CONFIRM_TICKS = 2
DEFAULT_TEMPORAL_WINDOW = 8


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class ExitIntelligenceEngine:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(config or {})
        self._exit_confirm_ticks = int(
            self._cfg.get("exit_confirm_ticks", DEFAULT_EXIT_CONFIRM_TICKS)
        )
        self._temporal_window = int(
            self._cfg.get("temporal_window", DEFAULT_TEMPORAL_WINDOW)
        )
        self._momentum_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._temporal_window)
        )
        self._exit_pressure_streak: dict[str, int] = defaultdict(int)

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
        thesis: TradeThesis | None = None,
        thesis_eval: ThesisEvaluation | None = None,
        transaction_cost_pct: float = 0.0004,
        timestamp: datetime | None = None,
    ) -> ExitDecision:
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

        mom_score = self._momentum_numeric(market_state)
        hist = self._momentum_history[symbol]
        hist.append(mom_score)
        features["momentum_numeric"] = mom_score
        if len(hist) >= 3:
            decay = hist[0] - hist[-1]
            features["momentum_decay"] = decay
            if decay >= 0.25:
                reasons.append(
                    ExitReason(
                        "momentum_decaying",
                        f"Momentum trajectory decay={decay:.2f} over {len(hist)} ticks",
                        +0.06,
                    )
                )

        hard_invalidation = False
        if thesis_eval is not None:
            features["thesis_consistency"] = thesis_eval.consistency_score
            if thesis_eval.health == ThesisHealth.INVALIDATED:
                hard_invalidation = True
                reasons.append(
                    ExitReason(
                        "thesis_invalidated",
                        f"Thesis INVALIDATED: {', '.join(thesis_eval.breached_invalidations)}",
                        +0.35,
                    )
                )
            elif thesis_eval.health == ThesisHealth.DEGRADED:
                reasons.append(
                    ExitReason(
                        "thesis_degraded",
                        f"Thesis DEGRADED (consistency={thesis_eval.consistency_score:.2f})",
                        +0.12,
                    )
                )
            else:
                reasons.append(
                    ExitReason(
                        "thesis_intact",
                        f"Thesis INTACT (consistency={thesis_eval.consistency_score:.2f})",
                        -0.10,
                    )
                )

        reasons.extend(self._structure_reasons(side, market_state))
        reasons.extend(self._order_flow_reasons(side, market_state))
        reasons.extend(self._momentum_reasons(market_state))
        if market_state.structure == StructureBias.BROKEN:
            hard_invalidation = True

        ere, path_reasons = self._path_and_ere(
            side, current_price, path_plan, transaction_cost_pct
        )
        reasons.extend(path_reasons)
        features["expected_remaining_edge"] = ere

        if structural_stop is not None:
            stop_dist_pct = (
                abs(current_price - structural_stop.stop_price) / current_price
            )
            features["dist_to_structural_stop_pct"] = stop_dist_pct
            if stop_dist_pct < 0.003:
                reasons.append(
                    ExitReason(
                        "near_structural_stop",
                        f"Price within {stop_dist_pct:.2%} of structural invalidation",
                        +0.18,
                    )
                )

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
                    "low_reversal_risk", f"MarketState reversal_risk={rr:.2f}", -0.08
                )
            )

        raw_score = sum(r.weight for r in reasons)
        exit_score = _clamp01(0.5 + raw_score / 2.0)
        features["exit_score_raw"] = raw_score
        features["exit_score"] = exit_score

        if hard_invalidation or exit_score >= EXIT_SCORE_EXIT:
            self._exit_pressure_streak[symbol] += 1
        else:
            self._exit_pressure_streak[symbol] = 0
        streak = self._exit_pressure_streak[symbol]
        features["exit_pressure_streak"] = float(streak)

        action, reduce_frac = self._decide(
            exit_score, ere, hard_invalidation=hard_invalidation, streak=streak
        )
        notes.append(
            f"action={action.value} score={exit_score:.3f} ere={ere:.4f} "
            f"streak={streak} hard_inv={hard_invalidation}"
        )

        return ExitDecision(
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

    def reset_symbol(self, symbol: str) -> None:
        self._momentum_history.pop(symbol, None)
        self._exit_pressure_streak.pop(symbol, None)

    @staticmethod
    def _momentum_numeric(state: MarketState) -> float:
        return {
            MomentumState.STRONG: 0.9,
            MomentumState.MODERATING: 0.65,
            MomentumState.WEAK: 0.35,
            MomentumState.EXHAUSTED: 0.1,
        }.get(state.momentum, 0.5)

    def _structure_reasons(self, side: str, state: MarketState) -> list[ExitReason]:
        reasons: list[ExitReason] = []
        if state.structure == StructureBias.BROKEN:
            reasons.append(
                ExitReason("structure_broken", "Market structure marked BROKEN", +0.30)
            )
        elif side == "LONG" and state.structure == StructureBias.BEARISH:
            reasons.append(
                ExitReason("structure_against", "LONG but structure is BEARISH", +0.22)
            )
        elif side == "SHORT" and state.structure == StructureBias.BULLISH:
            reasons.append(
                ExitReason("structure_against", "SHORT but structure is BULLISH", +0.22)
            )
        elif (side == "LONG" and state.structure == StructureBias.BULLISH) or (
            side == "SHORT" and state.structure == StructureBias.BEARISH
        ):
            reasons.append(
                ExitReason(
                    "structure_aligned",
                    f"Structure {state.structure.value} supports position",
                    -0.12,
                )
            )
        return reasons

    def _order_flow_reasons(self, side: str, state: MarketState) -> list[ExitReason]:
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
        elif (side == "LONG" and of == OrderFlowBias.BUYER_DOMINANT) or (
            side == "SHORT" and of == OrderFlowBias.SELLER_DOMINANT
        ):
            reasons.append(
                ExitReason(
                    "of_supportive", f"Order-flow {of.value} supports position", -0.10
                )
            )
        return reasons

    def _momentum_reasons(self, state: MarketState) -> list[ExitReason]:
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
                    "momentum_weak", "Momentum WEAK (mild; needs confirmation)", +0.05
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
        if side == "LONG":
            upside_dests, downside_dests = plan.upside, plan.downside
        else:
            upside_dests, downside_dests = plan.downside, plan.upside

        def _soft_mass(dests: tuple) -> tuple[float, float]:
            if not dests:
                return 0.0, 0.0
            raw = [max(0.0, float(d.probability)) for d in dests]
            total = sum(raw)
            if total <= 1e-12:
                return 0.0, 0.0
            scale = 1.0 / total if total > 1.0 else 1.0
            expected = sum(
                (p * scale) * (abs(d.price - current_price) / current_price)
                for d, p in zip(dests, raw)
            )
            return expected, total * scale if total > 1.0 else total

        expected_gain, total_up = _soft_mass(upside_dests)
        expected_loss, total_down = _soft_mass(downside_dests)
        ere = expected_gain - expected_loss - cost_pct

        if total_up >= 0.55:
            reasons.append(
                ExitReason(
                    "path_upside_alive",
                    f"Upside path probability mass ≈ {total_up:.2f}",
                    -0.12,
                )
            )
        elif total_up <= 0.25 and total_down >= 0.40:
            reasons.append(
                ExitReason(
                    "path_upside_collapsed",
                    f"Upside mass {total_up:.2f}, downside {total_down:.2f}",
                    +0.18,
                )
            )
        if ere > ERE_HOLD_THRESHOLD:
            reasons.append(
                ExitReason("positive_ere", f"Heuristic remaining edge={ere:.4f}", -0.10)
            )
        elif ere < ERE_EXIT_THRESHOLD:
            reasons.append(
                ExitReason("negative_ere", f"Heuristic remaining edge={ere:.4f}", +0.15)
            )
        return ere, reasons

    def _decide(
        self,
        exit_score: float,
        ere: float,
        *,
        hard_invalidation: bool,
        streak: int,
    ) -> tuple[ExitAction, float]:
        if hard_invalidation and exit_score >= EXIT_SCORE_MANAGE:
            return ExitAction.EXIT, 1.0
        if (
            exit_score >= EXIT_SCORE_EXIT
            and ere <= ERE_HOLD_THRESHOLD
            and streak >= self._exit_confirm_ticks
        ):
            return ExitAction.EXIT, 1.0
        if exit_score >= EXIT_SCORE_MANAGE or (ere <= 0.0 and exit_score >= 0.30):
            frac = _clamp01((exit_score - 0.25) / 0.5)
            return ExitAction.MANAGE, round(max(0.25, min(0.75, frac)), 2)
        return ExitAction.HOLD, 0.0
