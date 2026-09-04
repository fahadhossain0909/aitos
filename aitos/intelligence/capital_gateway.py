"""Bridge scanner opportunities into the capital objective.

This module intentionally contains no exchange calls. It converts the generic
scanner output into the venue-neutral economic estimate required by the
capital objective, then ranks and allocates only eligible opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass

from aitos.intelligence.capital_objective import (
    CapitalAllocation,
    CapitalAllocator,
    CapitalDecision,
    CapitalObjective,
    OpportunityEstimate,
)


@dataclass(frozen=True)
class CapitalGatewayResult:
    decisions: tuple[CapitalDecision, ...]
    allocations: tuple[CapitalAllocation, ...]


class CapitalGateway:
    """Single decision boundary between strategy evidence and capital use."""

    def __init__(
        self,
        objective: CapitalObjective | None = None,
        allocator: CapitalAllocator | None = None,
    ) -> None:
        self.objective = objective or CapitalObjective()
        self.allocator = allocator or CapitalAllocator(self.objective)

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
