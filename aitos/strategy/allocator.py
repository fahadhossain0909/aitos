"""Risk-aware capital allocation across competing strategy families."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import CapitalRequest


@dataclass(frozen=True)
class Allocation:
    strategy_id: str
    notional: float


class CapitalAllocator:
    """Allocate a shared capital budget without coupling strategies together."""

    def __init__(self, total_capital: float, *, max_strategy_fraction: float = 0.50) -> None:
        if total_capital < 0:
            raise ValueError("total_capital must be non-negative")
        if not 0 < max_strategy_fraction <= 1:
            raise ValueError("max_strategy_fraction must be in (0, 1]")
        self.total_capital = total_capital
        self.max_strategy_fraction = max_strategy_fraction

    def allocate(
        self,
        requests: list[CapitalRequest],
        *,
        reserved_capital: float = 0.0,
    ) -> tuple[Allocation, ...]:
        budget = max(0.0, self.total_capital - reserved_capital)
        if not requests or budget == 0:
            return ()
        ranked = sorted(
            requests,
            key=lambda r: (r.priority, r.expected_edge, r.confidence),
            reverse=True,
        )
        per_strategy_cap = self.total_capital * self.max_strategy_fraction
        allocations: list[Allocation] = []
        for request in ranked:
            if request.requested_notional <= 0 or request.max_loss < 0:
                continue
            amount = min(request.requested_notional, per_strategy_cap, budget)
            if amount <= 0:
                break
            allocations.append(Allocation(request.strategy_id, amount))
            budget -= amount
            if budget <= 0:
                break
        return tuple(allocations)
