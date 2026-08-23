"""Replayable lifecycle for historical limit-order queue simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Side = Literal["buy", "sell"]
Status = Literal["open", "partial", "filled", "cancelled", "expired"]


@dataclass
class SimulatedOrder:
    order_id: str
    side: Side
    price: float
    quantity: float
    remaining: float
    queue_ahead: float
    created_at: datetime
    status: Status = "open"
    last_queue_update: datetime | None = None
    ttl: timedelta | None = None


@dataclass(frozen=True)
class LifecycleFill:
    order_id: str
    quantity: float
    price: float
    timestamp: datetime
    maker: bool = True


class QueueOrderLifecycle:
    """Conservative passive-order lifecycle with book-change queue aging."""

    def __init__(self) -> None:
        self.orders: dict[str, SimulatedOrder] = {}

    def place(self, order: SimulatedOrder) -> None:
        if order.quantity <= 0 or order.remaining <= 0:
            raise ValueError("order quantity must be positive")
        if order.queue_ahead < 0:
            raise ValueError("queue_ahead must be non-negative")
        if order.order_id in self.orders:
            raise ValueError("duplicate order_id")
        order.last_queue_update = order.created_at
        self.orders[order.order_id] = order

    def cancel(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order is None or order.status in {"filled", "cancelled", "expired"}:
            return False
        order.status = "cancelled"
        return True

    def age(self, timestamp: datetime) -> list[str]:
        expired: list[str] = []
        for order in self.orders.values():
            if order.status not in {"open", "partial"} or order.ttl is None:
                continue
            if timestamp >= order.created_at + order.ttl:
                order.status = "expired"
                order.last_queue_update = timestamp
                expired.append(order.order_id)
        return expired

    def on_book_change(
        self,
        side: Side,
        price: float,
        old_qty: float,
        new_qty: float,
        timestamp: datetime,
    ) -> list[str]:
        """Apply displayed-volume reductions conservatively to queue ahead.

        Reductions are assigned to the earliest resting orders first. Increases
        never improve our queue position, preventing optimistic fills.
        """
        if old_qty < 0 or new_qty < 0:
            raise ValueError("book quantities must be non-negative")
        self.age(timestamp)
        reduction = max(0.0, old_qty - new_qty)
        updated: list[str] = []
        if reduction <= 0:
            return updated
        candidates = [
            o
            for o in self.orders.values()
            if o.status in {"open", "partial"} and o.side == side and o.price == price
        ]
        candidates.sort(key=lambda o: (o.created_at, o.order_id))
        for order in candidates:
            if reduction <= 0:
                break
            consumed = min(order.queue_ahead, reduction)
            if consumed > 0:
                order.queue_ahead -= consumed
                order.last_queue_update = timestamp
                reduction -= consumed
                updated.append(order.order_id)
        return updated

    def consume(
        self, side: Side, price: float, traded_qty: float, timestamp: datetime
    ) -> list[LifecycleFill]:
        if traded_qty <= 0:
            return []
        self.age(timestamp)
        fills: list[LifecycleFill] = []
        remaining_trade = traded_qty
        candidates = [
            o
            for o in self.orders.values()
            if o.status in {"open", "partial"} and o.side == side and o.price == price
        ]
        candidates.sort(key=lambda o: (o.created_at, o.order_id))
        for order in candidates:
            if remaining_trade <= 0:
                break
            if order.queue_ahead > 0:
                consumed = min(order.queue_ahead, remaining_trade)
                order.queue_ahead -= consumed
                remaining_trade -= consumed
            if order.queue_ahead > 0 or remaining_trade <= 0:
                continue
            fill_qty = min(order.remaining, remaining_trade)
            order.remaining -= fill_qty
            remaining_trade -= fill_qty
            order.status = "filled" if order.remaining <= 1e-12 else "partial"
            order.last_queue_update = timestamp
            fills.append(
                LifecycleFill(order.order_id, fill_qty, price, timestamp, maker=True)
            )
        return fills


@dataclass(frozen=True)
class FeeSchedule:
    maker_rate: float = 0.0002
    taker_rate: float = 0.0004

    def __post_init__(self) -> None:
        if self.maker_rate < 0 or self.taker_rate < 0:
            raise ValueError("fee rates must be non-negative")

    def fee(self, notional: float, maker: bool) -> float:
        if notional < 0:
            raise ValueError("notional must be non-negative")
        return notional * (self.maker_rate if maker else self.taker_rate)
