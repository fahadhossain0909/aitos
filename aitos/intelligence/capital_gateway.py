"""Bridge strategy opportunities into the capital objective."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from aitos.intelligence.capital_objective import (
    CapitalAllocation,
    CapitalAllocator,
    CapitalDecision,
    CapitalObjective,
    OpportunityEstimate,
)
from aitos.models.trade import Opportunity, TradeSide


@dataclass(frozen=True)
class CapitalGatewayResult:
    decisions: tuple[CapitalDecision, ...]
    allocations: tuple[CapitalAllocation, ...]

    def allocation_for(self, symbol: str) -> CapitalAllocation | None:
        return next((a for a in self.allocations if a.symbol == symbol), None)


class CapitalGateway:
    """Single decision boundary between strategy evidence and capital use."""

    def __init__(
        self,
        objective: CapitalObjective | None = None,
        allocator: CapitalAllocator | None = None,
    ) -> None:
        self.objective = objective or CapitalObjective()
        self.allocator = allocator or CapitalAllocator(self.objective)

    @staticmethod
    def estimate_opportunity(
        opportunity: Opportunity,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        funding_bps: float = 0.0,
    ) -> OpportunityEstimate:
        """Convert an executable opportunity into a conservative economic estimate.

        The nearest take-profit is used rather than the most optimistic target.
        Confidence is treated as a probability-like signal only at this boundary;
        calibrated probability estimates can override it through ``agent_consensus``.
        """
        entry = float(opportunity.entry_price)
        if not isfinite(entry) or entry <= 0:
            raise ValueError("opportunity entry_price must be finite and positive")
        if not opportunity.take_profit_levels:
            raise ValueError("opportunity requires at least one take-profit level")

        tp = float(opportunity.take_profit_levels[0])
        stop = float(opportunity.stop_loss_price)
        if not isfinite(tp) or not isfinite(stop) or tp <= 0 or stop <= 0:
            raise ValueError("opportunity TP/SL prices must be finite and positive")

        if opportunity.side == TradeSide.LONG:
            gross = (tp - entry) / entry * 100.0
        else:
            gross = (entry - tp) / entry * 100.0
        expected_loss = abs(entry - stop) / entry * 100.0

        consensus: dict[str, Any] = opportunity.agent_consensus
        probability = consensus.get("loss_probability")
        if probability is None:
            probability = 1.0 - float(opportunity.confidence)
        loss_probability = max(0.0, min(1.0, float(probability)))
        liquidity_score = float(consensus.get("liquidity_score", 5.0))
        regime_fit = float(consensus.get("regime_fit_score", 5.0))

        return OpportunityEstimate(
            symbol=opportunity.symbol,
            expected_gross_return_pct=max(0.0, gross),
            expected_loss_pct=max(0.0, expected_loss),
            loss_probability=loss_probability,
            fee_bps=max(0.0, float(fee_bps)),
            slippage_bps=max(0.0, float(slippage_bps)),
            funding_bps=max(0.0, float(funding_bps)),
            liquidity_score=max(0.0, liquidity_score),
            confidence=max(0.0, min(1.0, float(opportunity.confidence))),
            regime_fit=max(0.0, min(10.0, regime_fit)),
            metadata={"opportunity_id": opportunity.opportunity_id},
        )

    def evaluate(
        self,
        equity_usd: float,
        estimates: list[OpportunityEstimate],
        *,
        max_positions: int = 3,
    ) -> CapitalGatewayResult:
        decisions = self.objective.rank(estimates)
        allocations = self.allocator.allocate(
            equity_usd, decisions, max_positions=max_positions
        )
        return CapitalGatewayResult(tuple(decisions), tuple(allocations))

    def authorize_opportunity(
        self,
        equity_usd: float,
        opportunity: Opportunity,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        funding_bps: float = 0.0,
    ) -> tuple[CapitalDecision, CapitalAllocation | None]:
        """Return the capital decision and allocation for one opportunity."""
        estimate = self.estimate_opportunity(
            opportunity,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            funding_bps=funding_bps,
        )
        result = self.evaluate(equity_usd, [estimate], max_positions=1)
        decision = result.decisions[0]
        return decision, result.allocation_for(opportunity.symbol)
