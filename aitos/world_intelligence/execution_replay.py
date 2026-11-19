"""Deterministic venue-aware event execution replay.

Uses timestamped visible L2 snapshots and explicit latency, maker/taker fees,
funding observations, partial fills and square-root impact. It deliberately
reports unfilled quantity and does not assume hidden liquidity or queue priority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite, sqrt
from typing import Literal, Sequence

Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class ReplayBookLevel:
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class ReplayBookSnapshot:
    timestamp: datetime
    bids: tuple[ReplayBookLevel, ...]
    asks: tuple[ReplayBookLevel, ...]

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            raise ValueError("book timestamp must be timezone-aware")
        for level in (*self.bids, *self.asks):
            if not isfinite(level.price) or not isfinite(level.quantity) or level.price <= 0 or level.quantity < 0:
                raise ValueError("book levels must have positive prices and non-negative quantities")


@dataclass(frozen=True, slots=True)
class ReplayOrder:
    order_id: str
    side: Side
    quantity: float
    created_at: datetime
    max_wait: timedelta = timedelta(seconds=5)
    liquidity: Literal["maker", "taker"] = "taker"

    def __post_init__(self):
        if not self.order_id.strip() or self.side not in {"buy", "sell"}:
            raise ValueError("order identity and side are required")
        if not isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        if self.created_at.tzinfo is None or self.max_wait <= timedelta(0):
            raise ValueError("order time must be aware and max_wait positive")


@dataclass(frozen=True, slots=True)
class FundingObservation:
    timestamp: datetime
    rate: float

    def __post_init__(self):
        if self.timestamp.tzinfo is None or not isfinite(self.rate):
            raise ValueError("funding timestamp must be aware and rate finite")


@dataclass(frozen=True, slots=True)
class ReplayFill:
    order_id: str
    side: Side
    quantity: float
    price: float
    fee: float
    impact_bps: float
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReplayReport:
    order_count: int
    fill_count: int
    requested_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    fill_ratio: float
    average_fill_price: float | None
    fees_paid: float
    funding_paid: float
    estimated_market_impact: float
    fills: tuple[ReplayFill, ...]


@dataclass(frozen=True, slots=True)
class ExecutionReplayConfig:
    latency: timedelta = timedelta(milliseconds=100)
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    impact_coefficient_bps: float = 10.0
    max_levels: int = 20

    def __post_init__(self):
        if self.latency < timedelta(0) or self.max_levels < 1:
            raise ValueError("latency must be non-negative and max_levels positive")
        for value in (self.maker_fee_rate, self.taker_fee_rate, self.impact_coefficient_bps):
            if not isfinite(value) or value < 0:
                raise ValueError("execution costs must be finite and non-negative")


def replay_execution(
    orders: Sequence[ReplayOrder], books: Sequence[ReplayBookSnapshot], *,
    funding: Sequence[FundingObservation] = (), config: ExecutionReplayConfig = ExecutionReplayConfig(),
    position_side: Literal["long", "short"] = "long",
) -> ExecutionReplayReport:
    if len({o.order_id for o in orders}) != len(orders):
        raise ValueError("duplicate replay order id")
    ordered_books = sorted(books, key=lambda b: b.timestamp)
    fills = []
    fees = 0.0
    impact_total = 0.0
    requested = sum(o.quantity for o in orders)
    unfilled = 0.0
    for order in sorted(orders, key=lambda o: (o.created_at, o.order_id)):
        eligible_at = order.created_at + config.latency
        book = next((b for b in ordered_books if eligible_at <= b.timestamp <= eligible_at + order.max_wait), None)
        if book is None:
            unfilled += order.quantity
            continue
        levels = (book.asks if order.side == "buy" else book.bids)[:config.max_levels]
        remaining = order.quantity
        displayed_depth = sum(max(0.0, level.quantity) for level in levels)
        participation = order.quantity / max(displayed_depth, 1e-12)
        impact_bps = config.impact_coefficient_bps * sqrt(max(0.0, participation))
        for level in levels:
            if remaining <= 0:
                break
            qty = min(remaining, level.quantity)
            if qty <= 0:
                continue
            adverse = 1.0 + impact_bps / 10_000 if order.side == "buy" else 1.0 - impact_bps / 10_000
            price = level.price * adverse
            fee_rate = config.maker_fee_rate if order.liquidity == "maker" else config.taker_fee_rate
            fee = qty * price * fee_rate
            fills.append(ReplayFill(order.order_id, order.side, qty, price, fee, impact_bps, book.timestamp))
            fees += fee
            impact_total += abs(price - level.price) * qty
            remaining -= qty
        unfilled += remaining
    filled = sum(f.quantity for f in fills)
    notional = sum(f.quantity * f.price for f in fills)
    # Funding is charged on the replayed filled notional at each supplied funding timestamp.
    funding_paid = 0.0
    for observation in funding:
        if observation.timestamp.tzinfo is None:
            raise ValueError("funding timestamp must be timezone-aware")
        signed_rate = observation.rate if position_side == "long" else -observation.rate
        funding_paid += notional * signed_rate
    return ExecutionReplayReport(
        order_count=len(orders), fill_count=len(fills), requested_quantity=requested,
        filled_quantity=filled, unfilled_quantity=unfilled,
        fill_ratio=filled / requested if requested else 1.0,
        average_fill_price=notional / filled if filled else None,
        fees_paid=fees, funding_paid=funding_paid,
        estimated_market_impact=impact_total, fills=tuple(fills),
    )


__all__ = ["ExecutionReplayConfig", "ExecutionReplayReport", "FundingObservation",
           "ReplayBookLevel", "ReplayBookSnapshot", "ReplayFill", "ReplayOrder", "replay_execution"]
