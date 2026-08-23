"""Conservative interaction signals between executed flow and visible liquidity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aitos.intelligence.footprint_signals import FootprintSignals
from aitos.intelligence.liquidity_tracker import LiquidityEvent


@dataclass(frozen=True)
class FlowLiquiditySignal:
    kind: str
    direction: str
    score: float
    evidence: tuple[str, ...]


class FlowLiquidityInteractionEngine:
    """Join footprint evidence with recent L2 events without inventing causality."""

    def evaluate(
        self,
        footprint: FootprintSignals,
        liquidity_events: Iterable[LiquidityEvent],
    ) -> FlowLiquiditySignal:
        events = list(liquidity_events)
        score = 0.0
        evidence: list[str] = []

        ask_sweep = max(
            (e.score for e in events if e.kind == "sweep" and e.side == "ask"),
            default=0.0,
        )
        bid_sweep = max(
            (e.score for e in events if e.kind == "sweep" and e.side == "bid"),
            default=0.0,
        )
        ask_pull = max(
            (e.score for e in events if e.kind == "pulling" and e.side == "ask"),
            default=0.0,
        )
        bid_pull = max(
            (e.score for e in events if e.kind == "pulling" and e.side == "bid"),
            default=0.0,
        )
        ask_stack = max(
            (e.score for e in events if e.kind == "stacking" and e.side == "ask"),
            default=0.0,
        )
        bid_stack = max(
            (e.score for e in events if e.kind == "stacking" and e.side == "bid"),
            default=0.0,
        )

        # Aggressive buying + ask replenishment/stacking is a conservative absorption proxy.
        if footprint.bias == "bullish" and (
            ask_stack > 0 or footprint.absorption_score >= 6.0
        ):
            score = min(10.0, max(6.0, footprint.absorption_score + ask_stack * 0.35))
            evidence.append("bullish executed flow met persistent ask liquidity")
            return FlowLiquiditySignal(
                "buy_absorption_proxy", "bearish", score, tuple(evidence)
            )

        # Aggressive selling + bid replenishment/stacking is a conservative absorption proxy.
        if footprint.bias == "bearish" and (
            bid_stack > 0 or footprint.absorption_score >= 6.0
        ):
            score = min(10.0, max(6.0, footprint.absorption_score + bid_stack * 0.35))
            evidence.append("bearish executed flow met persistent bid liquidity")
            return FlowLiquiditySignal(
                "sell_absorption_proxy", "bullish", score, tuple(evidence)
            )

        # Sweep direction follows aggressive flow only when both signals agree.
        if ask_sweep >= 5.0 and footprint.bias == "bullish":
            score = min(
                10.0,
                (ask_sweep + footprint.delta_score + footprint.imbalance_score) / 3.0,
            )
            evidence.append("aggressive buying coincided with ask-liquidity removal")
            return FlowLiquiditySignal(
                "buy_side_sweep", "bullish", score, tuple(evidence)
            )

        if bid_sweep >= 5.0 and footprint.bias == "bearish":
            score = min(
                10.0,
                (
                    bid_sweep
                    + (10.0 - footprint.delta_score)
                    + (10.0 - footprint.imbalance_score)
                )
                / 3.0,
            )
            evidence.append("aggressive selling coincided with bid-liquidity removal")
            return FlowLiquiditySignal(
                "sell_side_sweep", "bearish", score, tuple(evidence)
            )

        # Liquidity pulling without flow confirmation is informational, not a trade signal.
        if ask_pull >= 5.0 and footprint.bias == "bullish":
            evidence.append("ask liquidity pulled while executed flow was bullish")
        if bid_pull >= 5.0 and footprint.bias == "bearish":
            evidence.append("bid liquidity pulled while executed flow was bearish")

        return FlowLiquiditySignal("none", "neutral", 0.0, tuple(evidence))
