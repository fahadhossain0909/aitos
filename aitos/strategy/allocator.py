"""Risk-aware batch capital allocation across competing strategies."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .contracts import CapitalRequest


@dataclass(frozen=True)
class Allocation:
    strategy_id: str
    notional: float
    projected_loss: float = 0.0
    allocation_weight: float = 0.0


class CapitalAllocator:
    """Allocate capital pro-rata by opportunity quality under shared risk caps.

    Requests are evaluated as one batch, not greedily in input order. Per-
    strategy concentration, available capital, and aggregate projected-loss
    budgets are all enforced before allocations are returned.
    """

    def __init__(
        self, total_capital: float, *, max_strategy_fraction: float = 0.50
    ) -> None:
        if not isfinite(total_capital) or total_capital < 0:
            raise ValueError("total_capital must be finite and non-negative")
        if not 0 < max_strategy_fraction <= 1:
            raise ValueError("max_strategy_fraction must be in (0, 1]")
        self.total_capital = float(total_capital)
        self.max_strategy_fraction = float(max_strategy_fraction)

    @staticmethod
    def _weight(request: CapitalRequest) -> float:
        # Shift signed edge into a non-negative score; neutral requests retain
        # a small exploration weight but cannot dominate a positive edge.
        edge = max(0.0, min(10.0, float(request.expected_edge)))
        confidence = max(0.0, min(1.0, float(request.confidence)))
        priority = max(0.0, min(10.0, float(request.priority)))
        return max(0.01, (0.25 + edge) * (0.25 + confidence) * (1.0 + priority))

    def allocate(
        self,
        requests: list[CapitalRequest],
        *,
        reserved_capital: float = 0.0,
        available_capital: float | None = None,
        risk_budget: float | None = None,
    ) -> tuple[Allocation, ...]:
        if not isfinite(reserved_capital) or reserved_capital < 0:
            raise ValueError("reserved_capital must be finite and non-negative")
        budget = max(0.0, self.total_capital - reserved_capital)
        if available_capital is not None:
            if not isfinite(available_capital) or available_capital < 0:
                raise ValueError("available_capital must be finite and non-negative")
            budget = min(budget, available_capital)
        if risk_budget is not None:
            if not isfinite(risk_budget) or risk_budget < 0:
                raise ValueError("risk_budget must be finite and non-negative")
        if not requests or budget <= 0 or risk_budget == 0:
            return ()

        eligible = []
        seen: set[str] = set()
        for request in requests:
            values = (
                request.requested_notional,
                request.max_loss,
                request.expected_edge,
                request.confidence,
                request.priority,
            )
            if not all(isfinite(float(value)) for value in values):
                continue
            if (
                not request.strategy_id
                or request.strategy_id in seen
                or request.requested_notional <= 0
                or request.max_loss < 0
            ):
                continue
            seen.add(request.strategy_id)
            cap = min(
                float(request.requested_notional),
                self.total_capital * self.max_strategy_fraction,
            )
            loss_rate = float(request.max_loss) / float(request.requested_notional)
            eligible.append((request, cap, loss_rate, self._weight(request)))

        if not eligible:
            return ()

        # Weighted water-filling: saturated requests leave the pool and their
        # unused capital is redistributed in the same batch.
        remaining = budget
        amounts = {request.strategy_id: 0.0 for request, *_ in eligible}
        active = list(eligible)
        while active and remaining > 1e-9:
            total_weight = sum(item[3] for item in active)
            if total_weight <= 0:
                break
            distributed = 0.0
            next_active = []
            for request, cap, loss_rate, weight in active:
                current = amounts[request.strategy_id]
                share = remaining * weight / total_weight
                grant = min(share, max(0.0, cap - current))
                amounts[request.strategy_id] = current + grant
                distributed += grant
                if current + grant < cap - 1e-9:
                    next_active.append((request, cap, loss_rate, weight))
            if distributed <= 1e-9:
                break
            remaining -= distributed
            active = next_active

        projected_risk = sum(
            amounts[request.strategy_id] * loss_rate
            for request, _, loss_rate, _ in eligible
        )
        if risk_budget is not None and projected_risk > risk_budget:
            scale = risk_budget / projected_risk if projected_risk > 0 else 0.0
            amounts = {key: value * scale for key, value in amounts.items()}

        allocations = []
        for request, _, loss_rate, weight in eligible:
            amount = amounts[request.strategy_id]
            if amount <= 1e-9:
                continue
            allocations.append(
                Allocation(
                    strategy_id=request.strategy_id,
                    notional=round(amount, 8),
                    projected_loss=round(amount * loss_rate, 8),
                    allocation_weight=round(weight, 8),
                )
            )
        return tuple(sorted(allocations, key=lambda item: item.strategy_id))
