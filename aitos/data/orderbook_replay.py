"""Deterministic L2 order-book reconstruction from snapshots and updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from .schema import CanonicalBookEvent


@dataclass(frozen=True)
class BookState:
    bids: Mapping[float, float]
    asks: Mapping[float, float]
    last_update_id: int | str | None


class OrderBookReconstructor:
    """Maintain a price-level book by applying absolute-size L2 updates.

    An update with quantity 0 removes the price level. Updates are rejected
    when their numeric sequence is older than the current update id.
    """

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: int | str | None = None

    @staticmethod
    def _numeric(value: int | str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def load_snapshot(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        update_id: int | str | None = None,
    ) -> None:
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self.last_update_id = update_id

    def apply(self, event: CanonicalBookEvent) -> bool:
        previous = self._numeric(self.last_update_id)
        current = self._numeric(event.update_id)
        if previous is not None and current is not None and current < previous:
            return False
        book = self.bids if event.side == "buy" else self.asks
        price = float(event.price)
        quantity = float(event.quantity)
        if quantity <= 0:
            book.pop(price, None)
        else:
            book[price] = quantity
        self.last_update_id = event.update_id
        return True

    def apply_many(self, events: Iterable[CanonicalBookEvent]) -> int:
        applied = 0
        for event in events:
            applied += int(self.apply(event))
        return applied

    def state(self) -> BookState:
        return BookState(dict(self.bids), dict(self.asks), self.last_update_id)

    def best_bid(self) -> tuple[float, float] | None:
        return max(self.bids.items(), key=lambda item: item[0]) if self.bids else None

    def best_ask(self) -> tuple[float, float] | None:
        return min(self.asks.items(), key=lambda item: item[0]) if self.asks else None

    def depth(
        self, levels: int = 10
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        if levels <= 0:
            return [], []
        bids = sorted(self.bids.items(), reverse=True)[:levels]
        asks = sorted(self.asks.items())[:levels]
        return bids, asks


def replay_snapshot_then_updates(
    snapshot: tuple[
        Iterable[tuple[float, float]], Iterable[tuple[float, float]], int | str | None
    ],
    updates: Iterable[CanonicalBookEvent],
) -> Iterator[BookState]:
    """Yield book state after the snapshot and after each accepted update."""
    book = OrderBookReconstructor()
    book.load_snapshot(*snapshot)
    yield book.state()
    for event in updates:
        if book.apply(event):
            yield book.state()
