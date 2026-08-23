"""Binance diff-depth local order-book reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple

from aitos.models.market import OrderBookSnapshot


@dataclass(frozen=True)
class DepthUpdate:
    first_update_id: int
    final_update_id: int
    previous_update_id: int
    bids: Tuple[Tuple[float, float], ...]
    asks: Tuple[Tuple[float, float], ...]
    event_time_ms: int


class OrderBookSequenceError(RuntimeError):
    """Raised when a diff-depth stream cannot be applied contiguously."""


class LocalOrderBook:
    def __init__(self, symbol: str, max_levels: int = 1000) -> None:
        self.symbol = symbol
        self.max_levels = max(20, max_levels)
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self.last_update_id: Optional[int] = None
        self.initialized = False
        self._awaiting_first_update = False

    def seed(self, snapshot: OrderBookSnapshot) -> None:
        self._bids = {p: q for p, q in snapshot.bids if q > 0}
        self._asks = {p: q for p, q in snapshot.asks if q > 0}
        self.last_update_id = snapshot.last_update_id
        self.initialized = True
        self._awaiting_first_update = True

    def apply(self, update: DepthUpdate) -> OrderBookSnapshot:
        if not self.initialized or self.last_update_id is None:
            raise OrderBookSequenceError(
                "order book must be seeded from REST snapshot first"
            )
        if update.final_update_id <= self.last_update_id:
            return self.snapshot(update.event_time_ms)
        if self._awaiting_first_update:
            if not (
                update.first_update_id
                <= self.last_update_id + 1
                <= update.final_update_id
            ):
                raise OrderBookSequenceError(
                    f"first diff does not bridge snapshot for {self.symbol}: snapshot={self.last_update_id}, U={update.first_update_id}, u={update.final_update_id}"
                )
            self._awaiting_first_update = False
        else:
            # For Binance Futures diff-depth, `pu` is the authoritative link
            # to the previously applied event. Do not independently require
            # U == local + 1: an event may span a range of update IDs, while
            # pu == local still proves that no event boundary was skipped.
            if update.previous_update_id != self.last_update_id:
                raise OrderBookSequenceError(
                    f"depth chain break for {self.symbol}: pu={update.previous_update_id}, local={self.last_update_id}"
                )
        self._apply_levels(self._bids, update.bids)
        self._apply_levels(self._asks, update.asks)
        self.last_update_id = update.final_update_id
        return self.snapshot(update.event_time_ms)

    @staticmethod
    def _apply_levels(
        book: Dict[float, float], levels: Iterable[Tuple[float, float]]
    ) -> None:
        for price, quantity in levels:
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def snapshot(self, event_time_ms: int = 0) -> OrderBookSnapshot:
        bids = tuple(
            sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[
                : self.max_levels
            ]
        )
        asks = tuple(sorted(self._asks.items(), key=lambda x: x[0])[: self.max_levels])
        timestamp = (
            datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc)
            if event_time_ms
            else datetime.now(timezone.utc)
        )
        return OrderBookSnapshot(
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            last_update_id=self.last_update_id or 0,
            timestamp=timestamp,
        )

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.last_update_id = None
        self.initialized = False
        self._awaiting_first_update = False
