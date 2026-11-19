"""Event-time execution replay using historical top-of-book snapshots."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class BookSnapshot:
    timestamp_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def __post_init__(self):
        values = (self.bid, self.ask, self.bid_size, self.ask_size)
        if self.timestamp_ms < 0 or any(not math.isfinite(float(x)) for x in values):
            raise ValueError(
                "book snapshot values must be finite and timestamp non-negative"
            )
        if (
            self.bid <= 0
            or self.ask < self.bid
            or self.bid_size < 0
            or self.ask_size < 0
        ):
            raise ValueError("invalid top-of-book quote or displayed size")


@dataclass(frozen=True)
class ReplayFill:
    timestamp_ms: int
    quantity: float
    quote_price: float
    fill_price: float
    fee: float
    slippage_cost: float


@dataclass(frozen=True)
class ExecutionReplayResult:
    side: str
    requested_quantity: float
    filled_quantity: float
    remaining_quantity: float
    average_fill_price: float | None
    fees: float
    slippage_cost: float
    spread_cost: float
    funding_cost: float
    borrow_cost: float
    total_cost: float
    fills: tuple[ReplayFill, ...]
    latency_ms: int
    complete: bool
