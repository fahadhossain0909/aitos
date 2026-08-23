"""Simple, deterministic L2-aware execution model for historical replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class L2Fill:
    side: Side
    requested_quantity: float
    filled_quantity: float
    average_price: float
    remaining_quantity: float
    notional: float


class L2ExecutionModel:
    """Consumes visible historical book liquidity across price levels.

    This models marketable execution against the visible snapshot. It does not
    model hidden liquidity, queue position, or exchange matching-engine priority.
    """

    def __init__(self, max_levels: int | None = None) -> None:
        if max_levels is not None and max_levels <= 0:
            raise ValueError("max_levels must be positive")
        self.max_levels = max_levels

    def execute(
        self, side: Side, quantity: float, bids: list[BookLevel], asks: list[BookLevel]
    ) -> L2Fill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        levels = asks if side == "buy" else bids
        levels = sorted(levels, key=lambda x: x.price, reverse=side == "sell")
        if self.max_levels is not None:
            levels = levels[: self.max_levels]
        remaining = quantity
        notional = 0.0
        filled = 0.0
        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, max(0.0, level.quantity))
            filled += take
            remaining -= take
            notional += take * level.price
        average = notional / filled if filled else 0.0
        return L2Fill(side, quantity, filled, average, remaining, notional)
