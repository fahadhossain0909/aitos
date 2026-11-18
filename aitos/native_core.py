"""Optional Rust native boundary and behaviorally equivalent Python reference.

Prices and quantities crossing the native boundary are integer ticks/lots.
Use InstrumentScale to convert exchange decimal strings without float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

try:
    from aitos_native import FixedPointOrderBook as _RustOrderBook
except ImportError:
    _RustOrderBook = None


def _positive_step(value: str | Decimal, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive decimal")
    return result


def _to_units(value: str | Decimal, step: Decimal, name: str, *, allow_zero: bool) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not decimal_value.is_finite() or decimal_value < 0 or (not allow_zero and decimal_value == 0):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'} and finite")
    units = decimal_value / step
    integral = units.to_integral_value()
    if units != integral:
        raise ValueError(f"{name} is not aligned to instrument step {step}")
    return int(integral)


@dataclass(frozen=True, slots=True)
class InstrumentScale:
    """Exact exchange tick/lot conversion; never routes binary floats to Rust."""

    price_tick: Decimal
    quantity_step: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_tick", _positive_step(self.price_tick, "price_tick"))
        object.__setattr__(self, "quantity_step", _positive_step(self.quantity_step, "quantity_step"))

    @classmethod
    def from_exchange_filters(cls, filters) -> "InstrumentScale":
        """Build from AITOS SymbolFilters or a compatible object."""
        return cls(
            price_tick=Decimal(str(filters.tick_size)),
            quantity_step=Decimal(str(filters.step_size)),
        )

    def price_to_ticks(self, price: str | Decimal) -> int:
        return _to_units(price, self.price_tick, "price", allow_zero=False)

    def quantity_to_lots(self, quantity: str | Decimal) -> int:
        return _to_units(quantity, self.quantity_step, "quantity", allow_zero=True)

    def price_from_ticks(self, ticks: int) -> Decimal:
        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks <= 0:
            raise ValueError("price ticks must be a positive integer")
        return self.price_tick * ticks

    def quantity_from_lots(self, lots: int) -> Decimal:
        if isinstance(lots, bool) or not isinstance(lots, int) or lots < 0:
            raise ValueError("quantity lots must be a non-negative integer")
        return self.quantity_step * lots


class PythonFixedPointOrderBook:
    """Reference fixed-point book; prices are ticks and quantities are lots."""

    def __init__(
        self,
        snapshot_id: int,
        bids: Iterable[tuple[int, int]] = (),
        asks: Iterable[tuple[int, int]] = (),
    ):
        self.last_update_id = self._uint(snapshot_id, "snapshot_id")
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self._first_delta = True
        self._load(self.bids, bids)
        self._load(self.asks, asks)

    @staticmethod
    def _uint(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _validate(rows) -> None:
        for price, quantity in rows:
            if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
                raise ValueError("price units must be positive integers")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                raise ValueError("quantity units must be non-negative integers")

    def _load(self, target: dict[int, int], rows: Iterable[tuple[int, int]]) -> None:
        rows = list(rows)
        self._validate(rows)
        for price, quantity in rows:
            if quantity:
                target[price] = quantity

    def apply_delta(
        self,
        first_update_id: int,
        final_update_id: int,
        bids=(),
        asks=(),
        previous_update_id=None,
    ) -> bool:
        first = self._uint(first_update_id, "first_update_id")
        final = self._uint(final_update_id, "final_update_id")
        bid_rows = list(bids)
        ask_rows = list(asks)
        self._validate(bid_rows)
        self._validate(ask_rows)
        if first > final:
            raise ValueError("first_update_id exceeds final_update_id")
        if final <= self.last_update_id:
            return False
        if self._first_delta:
            bridge = self.last_update_id + 1
            if not first <= bridge <= final:
                raise ValueError("first delta does not bridge snapshot")
        elif previous_update_id != self.last_update_id:
            raise ValueError("order-book sequence gap")
        for price, quantity in bid_rows:
            if quantity == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = quantity
        for price, quantity in ask_rows:
            if quantity == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = quantity
        self.last_update_id = final
        self._first_delta = False
        return True

    def snapshot(
        self, depth: int
    ) -> tuple[int, list[tuple[int, int]], list[tuple[int, int]]]:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
            raise ValueError("depth must be positive")
        bids = sorted(self.bids.items(), reverse=True)[:depth]
        asks = sorted(self.asks.items())[:depth]
        return self.last_update_id, bids, asks

    def best_bid_ask(self) -> tuple[int | None, int | None]:
        return (
            max(self.bids) if self.bids else None,
            min(self.asks) if self.asks else None,
        )

    def depth_imbalance(self, depth: int) -> float:
        _, bids, asks = self.snapshot(depth)
        bid_qty = sum(quantity for _, quantity in bids)
        ask_qty = sum(quantity for _, quantity in asks)
        total = bid_qty + ask_qty
        return (bid_qty - ask_qty) / total if total else 0.0

    def microprice_ticks(self) -> float | None:
        bid, ask = self.best_bid_ask()
        if bid is None or ask is None:
            return None
        bid_qty, ask_qty = self.bids[bid], self.asks[ask]
        total = bid_qty + ask_qty
        if total == 0:
            return None
        return (ask * bid_qty + bid * ask_qty) / total


def native_available() -> bool:
    """Return whether the optional Rust extension is importable."""
    return _RustOrderBook is not None


def create_order_book(
    snapshot_id: int,
    bids=(),
    asks=(),
    *,
    prefer_native: bool = True,
):
    """Prefer the optional native implementation, otherwise use the reference."""
    book_type = (
        _RustOrderBook
        if prefer_native and _RustOrderBook is not None
        else PythonFixedPointOrderBook
    )
    return book_type(snapshot_id, list(bids), list(asks))
