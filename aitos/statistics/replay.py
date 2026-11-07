"""Event-time execution replay using historical top-of-book snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


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
    """Replay a marketable order against time-ordered historical L1 liquidity.

    Spread is measured from midpoint to executable bid/ask; additional
    slippage is applied adversely. Displayed top-of-book size bounds each
    partial fill. Fees apply only to executed notional. Funding/borrow are
    charged on executed notional for the supplied holding horizon.
    This is an L1 replay, not a full-depth market-impact or queue-position model.
    """
    direction = side.upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    numeric = (
        quantity,
        taker_fee_bps,
        slippage_bps,
        funding_bps,
        borrow_bps,
        holding_hours,
        funding_interval_hours,
    )
    if (
        any(not math.isfinite(float(x)) for x in numeric)
        or quantity <= 0
        or any(x < 0 for x in numeric[1:])
    ):
        raise ValueError("quantity and cost inputs must be finite and non-negative")
    if funding_interval_hours <= 0:
        raise ValueError("funding_interval_hours must be positive")
    if slippage_bps >= 10000:
        raise ValueError(
            "slippage_bps must be below 10000 to preserve positive fill prices"
        )
    if submitted_at_ms < 0 or latency_ms < 0:
        raise ValueError("timestamps and latency must be non-negative")
    ordered = tuple(snapshots)
    if any(
        ordered[i].timestamp_ms >= ordered[i + 1].timestamp_ms
        for i in range(len(ordered) - 1)
    ):
        raise ValueError("book snapshots must be strictly chronological")
    eligible_at = submitted_at_ms + latency_ms
    remaining = float(quantity)
    fills = []
    fees = slippage = spread = 0.0
    for quote in ordered:
        if quote.timestamp_ms < eligible_at or remaining <= 1e-12:
            continue
        available = quote.ask_size if direction == "BUY" else quote.bid_size
        fill_qty = min(remaining, available)
        if fill_qty <= 0:
            continue
        quote_price = quote.ask if direction == "BUY" else quote.bid
        mid = (quote.ask + quote.bid) / 2.0
        adverse = (
            quote_price * (1.0 + slippage_bps / 10000.0)
            if direction == "BUY"
            else quote_price * (1.0 - slippage_bps / 10000.0)
        )
        notional = fill_qty * adverse
        fee = notional * taker_fee_bps / 10000.0
        slip_cost = fill_qty * abs(adverse - quote_price)
        spread_cost = fill_qty * abs(quote_price - mid)
        fills.append(
            ReplayFill(
                quote.timestamp_ms, fill_qty, quote_price, adverse, fee, slip_cost
            )
        )
        fees += fee
        slippage += slip_cost
        spread += spread_cost
        remaining -= fill_qty
    filled = quantity - remaining
    notional = sum(fill.quantity * fill.fill_price for fill in fills)
    avg = notional / filled if filled > 0 else None
    funding = notional * funding_bps / 10000.0 * holding_hours / funding_interval_hours
    borrow = notional * borrow_bps / 10000.0 * holding_hours / 24.0
    total = fees + slippage + spread + funding + borrow
    return ExecutionReplayResult(
        direction,
        quantity,
        filled,
        remaining,
        avg,
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
