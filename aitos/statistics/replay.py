"""Event-time L1 execution replay using historical top-of-book snapshots.

This is a scenario/research model, not an exchange fill guarantee. Remaining
quantity is carried across eligible snapshots as an unfilled child; it does not
model queue priority, order cancellation, price limits or IOC semantics.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from numbers import Real
from numbers import Real


@dataclass(frozen=True)
class BookSnapshot:
    timestamp_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("book timestamp must be a non-negative integer")
        values = {
            "bid": self.bid,
            "ask": self.ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
        }
        for name, value in values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"book {name} must be finite numeric evidence")
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


def replay_historical_order(
    snapshots: Iterable[BookSnapshot],
    *,
    side: str,
    quantity: float,
    submitted_at_ms: int,
    latency_ms: int,
    taker_fee_bps: float,
    slippage_bps: float = 0.0,
    funding_bps: float = 0.0,
    borrow_bps: float = 0.0,
    holding_hours: float = 0.0,
    funding_interval_hours: float = 8.0,
) -> ExecutionReplayResult:
    """Replay an order over chronological L1 quotes after modeled latency.

    Displayed top-of-book size bounds each partial fill. The unfilled remainder
    remains eligible at later snapshots; callers must not interpret this as IOC
    or passive queue execution. Spread is measured from midpoint to executable
    bid/ask, additional slippage is applied adversely, and fees apply only to
    executed notional. Funding basis points are signed from the position
    perspective: negative values represent credits. Funding/borrow costs use
    executed notional and the supplied holding horizon. This is not full-depth
    market impact.
    """
    if not isinstance(side, str) or side.upper() not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    direction = side.upper()
    numeric = {
        "quantity": quantity,
        "taker_fee_bps": taker_fee_bps,
        "slippage_bps": slippage_bps,
        "funding_bps": funding_bps,
        "borrow_bps": borrow_bps,
        "holding_hours": holding_hours,
        "funding_interval_hours": funding_interval_hours,
    }
    for name, value in numeric.items():
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be a finite numeric value")
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite numeric value")
    if (
        quantity <= 0
        or taker_fee_bps < 0
        or slippage_bps < 0
        or slippage_bps >= 10000
        or borrow_bps < 0
        or holding_hours < 0
        or funding_interval_hours <= 0
    ):
        raise ValueError("invalid quantity, fee, slippage or holding horizon")
    if (
        isinstance(submitted_at_ms, bool)
        or not isinstance(submitted_at_ms, int)
        or isinstance(latency_ms, bool)
        or not isinstance(latency_ms, int)
        or submitted_at_ms < 0
        or latency_ms < 0
    ):
        raise ValueError("timestamps and latency must be non-negative integers")

    ordered = tuple(snapshots)
    if any(not isinstance(quote, BookSnapshot) for quote in ordered):
        raise TypeError("L1 replay requires BookSnapshot records")
    if any(
        left.timestamp_ms >= right.timestamp_ms
        for left, right in zip(ordered, ordered[1:])
    ):
        raise ValueError("book snapshots must be strictly chronological")

    eligible_at = submitted_at_ms + latency_ms
    remaining = float(quantity)
    fills: list[ReplayFill] = []
    fees = slippage = spread = 0.0
    for quote in ordered:
        if quote.timestamp_ms < eligible_at or remaining <= 1e-12:
            continue
        available = quote.ask_size if direction == "BUY" else quote.bid_size
        fill_quantity = min(remaining, available)
        if fill_quantity <= 0:
            continue
        quote_price = quote.ask if direction == "BUY" else quote.bid
        midpoint = (quote.ask + quote.bid) / 2.0
        fill_price = (
            quote_price * (1.0 + slippage_bps / 10000.0)
            if direction == "BUY"
            else quote_price * (1.0 - slippage_bps / 10000.0)
        )
        notional = fill_quantity * fill_price
        fee = notional * taker_fee_bps / 10000.0
        slippage_cost = fill_quantity * abs(fill_price - quote_price)
        spread_cost = fill_quantity * abs(quote_price - midpoint)
        fills.append(
            ReplayFill(
                quote.timestamp_ms,
                fill_quantity,
                quote_price,
                fill_price,
                fee,
                slippage_cost,
            )
        )
        fees += fee
        slippage += slippage_cost
        spread += spread_cost
        remaining -= fill_quantity

    filled = float(quantity) - remaining
    executed_notional = sum(fill.quantity * fill.fill_price for fill in fills)
    average_fill_price = executed_notional / filled if filled > 0 else None
    funding = (
        executed_notional
        * funding_bps
        / 10000.0
        * holding_hours
        / funding_interval_hours
    )
    borrow = executed_notional * borrow_bps / 10000.0 * holding_hours / 24.0
    total = fees + slippage + spread + funding + borrow
    return ExecutionReplayResult(
        direction,
        float(quantity),
        filled,
        remaining,
        average_fill_price,
        fees,
        slippage,
        spread,
        funding,
        borrow,
        total,
        tuple(fills),
        latency_ms,
        remaining <= 1e-12,
    )
