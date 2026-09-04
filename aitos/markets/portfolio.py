"""Universal portfolio and exposure primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .contracts import Instrument


@dataclass(frozen=True, slots=True)
class Position:
    instrument: Instrument
    quantity: float
    average_price: float
    mark_price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.mark_price * (self.instrument.contract_size or 1.0)

    @property
    def unrealized_pnl(self) -> float:
        return (self.mark_price - self.average_price) * self.quantity * (self.instrument.contract_size or 1.0)


@dataclass(slots=True)
class Portfolio:
    """Asset-class-neutral portfolio view used by every strategy."""

    positions: dict[str, Position] = field(default_factory=dict)
    cash: float = 0.0

    def upsert(self, position: Position) -> None:
        self.positions[position.instrument.id] = position

    def remove(self, instrument: Instrument) -> None:
        self.positions.pop(instrument.id, None)

    @property
    def gross_notional(self) -> float:
        return sum(abs(position.notional) for position in self.positions.values())

    @property
    def net_notional(self) -> float:
        return sum(position.notional for position in self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(position.unrealized_pnl for position in self.positions.values())

    def exposure_by_asset_class(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for position in self.positions.values():
            key = position.instrument.asset_class.value
            totals[key] = totals.get(key, 0.0) + position.notional
        return totals
