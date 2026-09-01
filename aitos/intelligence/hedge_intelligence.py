"""Conditional hedge intelligence for ambiguous/adverse market states.

The hedge is an overlay, never a replacement for the primary position. It
opens only when the market state is sufficiently conflicted/adverse and closes
when the primary thesis regains directional confirmation. The engine is
stateful per trade so repeated evaluations cannot stack hedges unintentionally.
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
    reason: str
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "hedge_side": self.hedge_side,
            "hedge_ratio": self.hedge_ratio,
            "score": self.score,
            "recovery_score": self.recovery_score,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class HedgeIntelligenceEngine:
    """Conservative, explainable hedge overlay.

    Scores are intentionally bounded and deterministic. A hedge requires both
    ambiguity/adverse evidence and a minimum score; it is never unconditional.
    """

    def __init__(
        self,
        *,
        open_threshold: float = 0.68,
        close_threshold: float = 0.56,
        max_ratio: float = 0.50,
        min_ratio: float = 0.20,
    ) -> None:
        self.open_threshold = open_threshold
        self.close_threshold = close_threshold
        self.max_ratio = max_ratio
        self.min_ratio = min_ratio
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
        adverse = (
            (trade.entry_price - current_price) / max(atr or 1.0, 1e-9)
            if primary_long
            else (current_price - trade.entry_price) / max(atr or 1.0, 1e-9)
        )
        adverse_score = min(max(adverse, 0.0) / 2.0, 1.0)

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

        score = min(1.0, 0.45 * conflict / 0.88 + 0.55 * adverse_score)
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

        if active and recovery_score >= self.close_threshold and adverse_score < 0.35:
            self._active.pop(trade.trade_id, None)
            return HedgeDecision(
                "CLOSE",
                None,
                0.0,
                score,
                recovery_score,
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
                "Hedge conditions not strong enough.",
                timestamp,
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
        self._active[trade.trade_id] = ratio
        return HedgeDecision(
            "OPEN",
            "SHORT" if primary_long else "LONG",
            ratio,
            score,
            recovery_score,
            "Conflicted/adverse state warrants a partial protective hedge.",
            timestamp,
        )
