"""Conditional hedge intelligence.

This engine is a risk overlay, not a directional entry strategy. It only
opens a partial opposite position while the primary thesis remains valid.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.exit_intelligence.models import ExitAction
from aitos.intelligence.market_state.models import MarketState
from aitos.intelligence.trade_thesis.models import ThesisEvaluation, ThesisHealth

from .models import HedgeAction, HedgeDecision


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class HedgeIntelligenceEngine:
    """Deterministic conditional hedge decision engine."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.open_threshold = float(cfg.get("open_threshold", 0.75))
        self.candidate_threshold = float(cfg.get("candidate_threshold", 0.60))
        self.close_threshold = float(cfg.get("close_recovery_threshold", 0.65))
        self.max_hedge_ratio = _clamp01(float(cfg.get("max_hedge_ratio", 0.50)))
        self.min_hedge_ratio = _clamp01(float(cfg.get("min_hedge_ratio", 0.25)))
        self.min_expected_move_r = float(cfg.get("min_expected_move_r", 0.25))
        self.max_duration_seconds = int(cfg.get("max_duration_seconds", 1800))

    def evaluate(
        self,
        *,
        symbol: str,
        primary_side: str,
        market_state: MarketState,
        thesis_eval: ThesisEvaluation,
        exit_action: ExitAction,
        current_price: float,
        primary_entry_price: float,
        primary_r_distance: float = 0.0,
        hedge_active: bool = False,
        hedge_opened_at: datetime | None = None,
        timestamp: datetime | None = None,
    ) -> HedgeDecision:
        ts = timestamp or datetime.now(timezone.utc)
        side = primary_side.upper()
        hedge_side = "SHORT" if side == "LONG" else "LONG"
        features: dict[str, float] = {}
        reasons: list[str] = []

        if not self.enabled:
            return self._decision(
                symbol,
                side,
                HedgeAction.NONE,
                None,
                0.0,
                0.0,
                reasons,
                features,
                ts,
                ("hedge_disabled",),
            )

        if (
            thesis_eval.health == ThesisHealth.INVALIDATED
            or exit_action == ExitAction.EXIT
        ):
            reasons.append("primary_thesis_invalid_or_exit")
            action = HedgeAction.CLOSE if hedge_active else HedgeAction.NONE
            return self._decision(
                symbol,
                side,
                action,
                hedge_side if hedge_active else None,
                0.0,
                self._recovery_score(side, market_state),
                reasons,
                features,
                ts,
                ("primary_exit_has_priority",),
            )

        score = self._adverse_score(side, market_state)
        recovery = self._recovery_score(side, market_state)
        features.update({"hedge_score": score, "recovery_score": recovery})

        if hedge_active:
            close = recovery >= self.close_threshold
            if hedge_opened_at is not None:
                age = max(0.0, (ts - hedge_opened_at).total_seconds())
                features["hedge_age_seconds"] = age
                if age >= self.max_duration_seconds:
                    close = True
                    reasons.append("hedge_max_duration")
            if close:
                reasons.append("primary_direction_recovery")
                return self._decision(
                    symbol,
                    side,
                    HedgeAction.CLOSE,
                    hedge_side,
                    0.0,
                    recovery,
                    reasons,
                    features,
                    ts,
                    ("close_temporary_hedge",),
                )
            return self._decision(
                symbol,
                side,
                HedgeAction.HOLD,
                hedge_side,
                self._ratio(score),
                recovery,
                reasons,
                features,
                ts,
                ("hedge_still_protective",),
            )

        if exit_action == ExitAction.MANAGE:
            reasons.append("eie_manage_allows_risk_overlay")
        if score < self.candidate_threshold:
            return self._decision(
                symbol,
                side,
                HedgeAction.NONE,
                None,
                0.0,
                recovery,
                reasons,
                features,
                ts,
                (),
            )

        move = abs(current_price - primary_entry_price)
        r_distance = (
            primary_r_distance
            if primary_r_distance > 0
            else abs(primary_entry_price - current_price)
        )
        expected_r = move / max(r_distance, 1e-12)
        features["adverse_move_r"] = expected_r
        if score < self.open_threshold or expected_r < self.min_expected_move_r:
            reasons.append("hedge_candidate_below_open_threshold")
            return self._decision(
                symbol,
                side,
                HedgeAction.NONE,
                None,
                0.0,
                recovery,
                reasons,
                features,
                ts,
                (),
            )

        reasons.append("elevated_adverse_market_risk")
        return self._decision(
            symbol,
            side,
            HedgeAction.OPEN,
            hedge_side,
            self._ratio(score),
            recovery,
            reasons,
            features,
            ts,
            ("partial_temporary_hedge",),
        )

    def _adverse_score(self, side: str, state: MarketState) -> float:
        score = 0.0
        structure = getattr(state.structure, "value", str(state.structure)).upper()
        of = getattr(state.order_flow_bias, "value", str(state.order_flow_bias)).upper()
        momentum = getattr(state.momentum, "value", str(state.momentum)).upper()
        vol = getattr(
            state.volatility_regime, "value", str(state.volatility_regime)
        ).upper()
        liq = getattr(state.liquidity_bias, "value", str(state.liquidity_bias)).upper()
        if (side == "LONG" and of == "SELLER_DOMINANT") or (
            side == "SHORT" and of == "BUYER_DOMINANT"
        ):
            score += 0.30
        if (side == "LONG" and structure in {"BEARISH", "BROKEN"}) or (
            side == "SHORT" and structure in {"BULLISH", "BROKEN"}
        ):
            score += 0.25
        if momentum in {"WEAK", "EXHAUSTED", "MODERATING"}:
            score += 0.15
        if vol == "EXPANDING":
            score += 0.15
        if (side == "LONG" and liq == "DOWNSIDE_LIQUIDITY_HIGH") or (
            side == "SHORT" and liq == "UPSIDE_LIQUIDITY_HIGH"
        ):
            score += 0.10
        score += 0.05 * _clamp01(float(getattr(state, "reversal_risk", 0.0)))
        return _clamp01(score)

    def _recovery_score(self, side: str, state: MarketState) -> float:
        structure = getattr(state.structure, "value", str(state.structure)).upper()
        of = getattr(state.order_flow_bias, "value", str(state.order_flow_bias)).upper()
        momentum = getattr(state.momentum, "value", str(state.momentum)).upper()
        return _clamp01(
            (
                0.45
                if (
                    (side == "LONG" and of == "BUYER_DOMINANT")
                    or (side == "SHORT" and of == "SELLER_DOMINANT")
                )
                else 0.0
            )
            + (
                0.35
                if (
                    (side == "LONG" and structure == "BULLISH")
                    or (side == "SHORT" and structure == "BEARISH")
                )
                else 0.0
            )
            + (0.20 if momentum == "STRONG" else 0.0)
        )

    def _ratio(self, score: float) -> float:
        if self.max_hedge_ratio <= self.min_hedge_ratio:
            return self.max_hedge_ratio
        scale = _clamp01(
            (score - self.open_threshold) / max(1e-9, 1.0 - self.open_threshold)
        )
        return round(
            self.min_hedge_ratio
            + scale * (self.max_hedge_ratio - self.min_hedge_ratio),
            4,
        )

    @staticmethod
    def _decision(
        symbol: str,
        side: str,
        action: HedgeAction,
        hedge_side: str | None,
        ratio: float,
        recovery: float,
        reasons: list[str],
        features: dict[str, float],
        ts: datetime,
        notes: tuple[str, ...],
    ) -> HedgeDecision:
        return HedgeDecision(
            symbol=symbol,
            primary_side=side,
            action=action,
            hedge_side=hedge_side,
            hedge_ratio=ratio,
            hedge_score=features.get("hedge_score", 0.0),
            recovery_score=recovery,
            reason_codes=tuple(reasons),
            as_of=ts,
            features=dict(features),
            notes=notes,
        )
