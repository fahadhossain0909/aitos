"""Conservative queue-aware execution primitives for historical L2 replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class QueueLevel:
    price: float
    displayed_qty: float
    ahead_qty: float


@dataclass(frozen=True)
class QueueFill:
    side: Side
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    average_price: float
    notional: float


class QueueAwareExecutionModel:
    """Approximate queue priority using displayed volume ahead of our order.

    This is intentionally conservative: queue-ahead is supplied by the
    historical replay rather than guessed from current liquidity. Hidden
    liquidity and exchange-specific matching rules remain out of scope.
    """

    def execute(
        self, side: Side, quantity: float, levels: list[QueueLevel]
    ) -> QueueFill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        remaining = quantity
        filled = 0.0
        notional = 0.0
        for level in levels:
            if remaining <= 0:
                break
            available = max(0.0, level.displayed_qty - level.ahead_qty)
            take = min(remaining, available)
            filled += take
            remaining -= take
            notional += take * level.price
        average = notional / filled if filled else 0.0
        return QueueFill(side, quantity, filled, remaining, average, notional)
