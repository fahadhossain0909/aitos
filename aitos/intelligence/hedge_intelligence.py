"""Conditional hedge intelligence for ambiguous/adverse market states.

The hedge is an overlay, never a replacement for the primary position. It
opens only when the market state is sufficiently conflicted/adverse, the
expected protection justifies its round-trip cost, and a minimum score is met.
It closes when the primary thesis regains directional confirmation. The engine
is stateful per trade so repeated evaluations cannot stack hedges.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aitos.intelligence.market_state import (
    AuctionState,
    MarketState,
    MomentumState,
    OrderFlowBias,
    Regime,
    StructureBias,
    VolatilityRegime,
)
from aitos.models.trade import Trade, TradeSide


@dataclass(frozen=True)
class HedgeDecision:
    action: str  # OPEN, HOLD, CLOSE, NONE
    hedge_side: str | None
    hedge_ratio: float
    score: float
    recovery_score: float
    expected_benefit: float
    expected_cost: float
    benefit_cost_ratio: float
    reason: str
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "hedge_side": self.hedge_side,
            "hedge_ratio": self.hedge_ratio,
            "score": self.score,
            "recovery_score": self.recovery_score,
            "expected_benefit": self.expected_benefit,
            "expected_cost": self.expected_cost,
            "benefit_cost_ratio": self.benefit_cost_ratio,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class HedgeIntelligenceEngine:
    """Conservative, explainable and cost-aware hedge overlay."""

    def __init__(
        self,
        *,
        open_threshold: float = 0.68,
        close_threshold: float = 0.56,
        max_ratio: float = 0.50,
        min_ratio: float = 0.20,
        min_benefit_cost_ratio: float = 2.0,
        estimated_roundtrip_cost_rate: float = 0.0012,
    ) -> None:
        self.open_threshold = open_threshold
        self.close_threshold = close_threshold
        self.max_ratio = max_ratio
        self.min_ratio = min_ratio
        self.min_benefit_cost_ratio = min_benefit_cost_ratio
        self.estimated_roundtrip_cost_rate = estimated_roundtrip_cost_rate
        self._active: dict[str, float] = {}

    def reset(self, trade_id: str) -> None:
        self._active.pop(trade_id, None)

    def evaluate(
        self,
        *,
        trade: Trade,
        market_state: MarketState,
        current_price: float,
        timestamp: datetime,
        atr: float | None = None,
    ) -> HedgeDecision:
        primary_long = trade.side == TradeSide.LONG
        adverse_distance = (
            max(trade.entry_price - current_price, 0.0)
            if primary_long
            else max(current_price - trade.entry_price, 0.0)
        )
        adverse_atr = adverse_distance / max(atr or 1.0, 1e-9)
        adverse_score = min(adverse_atr / 2.0, 1.0)

        conflict = 0.0
        if market_state.regime in {Regime.RANGE, Regime.TRANSITION}:
            conflict += 0.22
        if market_state.volatility_regime == VolatilityRegime.EXPANDING:
            conflict += 0.14
        if market_state.order_flow_bias == OrderFlowBias.NEUTRAL:
            conflict += 0.10
        if market_state.momentum in {MomentumState.WEAK, MomentumState.EXHAUSTED}:
            conflict += 0.12
        if market_state.reversal_risk >= 0.55:
            conflict += 0.18
        if market_state.structure in {StructureBias.BROKEN, StructureBias.RANGE}:
            conflict += 0.12
        if market_state.auction_state in {
            AuctionState.REJECTION_OF_HIGHS,
            AuctionState.REJECTION_OF_LOWS,
        }:
            conflict += 0.08

        conflict_score = min(conflict / 0.88, 1.0)
        score = min(1.0, 0.45 * conflict_score + 0.55 * adverse_score)
        active = trade.trade_id in self._active

        primary_bias_aligned = (
            market_state.structure == StructureBias.BULLISH and primary_long
        ) or (market_state.structure == StructureBias.BEARISH and not primary_long)
        recovery_score = min(
            1.0,
            0.45 * float(primary_bias_aligned)
            + 0.30 * float(market_state.trend_strength >= 0.55)
            + 0.25 * float(market_state.reversal_risk <= 0.40),
        )

        ratio = min(
            self.max_ratio,
            max(
                self.min_ratio,
                self.min_ratio
                + 0.30
                * (score - self.open_threshold)
                / max(1.0 - self.open_threshold, 1e-9),
            ),
        )
        notional = max(current_price * trade.quantity * ratio, 0.0)
        continuation_score = min(
            1.0,
            0.65 * adverse_score + 0.35 * conflict_score,
        )
        expected_benefit = (
            adverse_distance * trade.quantity * ratio * continuation_score
        )
        expected_cost = notional * self.estimated_roundtrip_cost_rate
        benefit_cost_ratio = (
            expected_benefit / expected_cost if expected_cost > 0 else float("inf")
        )

        if active and recovery_score >= self.close_threshold and adverse_score < 0.35:
            self._active.pop(trade.trade_id, None)
            return HedgeDecision(
                "CLOSE",
                None,
                0.0,
                score,
                recovery_score,
                expected_benefit,
                expected_cost,
                benefit_cost_ratio,
                "Primary-direction confirmation recovered; close protective hedge.",
                timestamp,
            )

        if active:
            return HedgeDecision(
                "HOLD",
                "SHORT" if primary_long else "LONG",
                self._active[trade.trade_id],
                score,
                recovery_score,
                expected_benefit,
                expected_cost,
                benefit_cost_ratio,
                "Protective hedge remains active.",
                timestamp,
            )

        if score < self.open_threshold:
            return HedgeDecision(
                "NONE",
                None,
                0.0,
                score,
                recovery_score,
                expected_benefit,
                expected_cost,
                benefit_cost_ratio,
                "Hedge conditions not strong enough.",
                timestamp,
            )

        if benefit_cost_ratio < self.min_benefit_cost_ratio:
            return HedgeDecision(
                "NONE",
                None,
                0.0,
                score,
                recovery_score,
                expected_benefit,
                expected_cost,
                benefit_cost_ratio,
                "Expected hedge protection does not justify estimated round-trip cost.",
                timestamp,
            )

        self._active[trade.trade_id] = ratio
        return HedgeDecision(
            "OPEN",
            "SHORT" if primary_long else "LONG",
            ratio,
            score,
            recovery_score,
            expected_benefit,
            expected_cost,
            benefit_cost_ratio,
            "Adverse/conflicted state passes the economic hedge gate.",
            timestamp,
        )
