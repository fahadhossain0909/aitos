"""Optional Rust native boundary and behaviorally equivalent Python reference."""

from __future__ import annotations

from typing import Iterable

try:
    from aitos_native import FixedPointOrderBook as _RustOrderBook
except ImportError:
    _RustOrderBook = None


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
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity < 0
            ):
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
