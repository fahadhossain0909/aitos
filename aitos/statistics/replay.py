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
    market_impact_cost: float = 0.0


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: float

    def __post_init__(self):
        if isinstance(self.price, bool) or isinstance(self.quantity, bool):
            raise ValueError("depth levels require numeric price and size")
        try:
            price = float(self.price)
            quantity = float(self.quantity)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("depth levels require numeric price and size") from None
        if (
            not math.isfinite(price)
            or not math.isfinite(quantity)
            or price <= 0
            or quantity < 0
        ):
            raise ValueError("depth levels require a positive price and non-negative size")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)


@dataclass(frozen=True)
class DepthBookSnapshot:
    timestamp_ms: int
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]

    def __post_init__(self):
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("depth snapshot timestamp must be a non-negative integer")
        bids, asks = tuple(self.bids), tuple(self.asks)
        if not bids or not asks:
            raise ValueError("depth snapshot requires both book sides")
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)
        if any(not isinstance(level, DepthLevel) for level in (*bids, *asks)):
            raise TypeError("depth snapshot levels must be DepthLevel instances")
        if any(
            self.bids[index].price <= self.bids[index + 1].price
            for index in range(len(self.bids) - 1)
        ):
            raise ValueError("bid levels must be strictly descending")
        if any(
            self.asks[index].price >= self.asks[index + 1].price
            for index in range(len(self.asks) - 1)
        ):
            raise ValueError("ask levels must be strictly ascending")
        if self.bids[0].price > self.asks[0].price:
            raise ValueError("depth snapshot must not contain a crossed market")


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

def replay_historical_order_l2(
    snapshots: Iterable[DepthBookSnapshot],
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
    """Sweep one point-in-time L2 book after modeled order latency.

    The first snapshot available at or after arrival is consumed exactly once.
    The order walks displayed levels in price priority, so the difference
    between best quote and deeper execution levels is explicit market impact.
    Missing depth remains unfilled; later snapshots are not reused as if they
    were the same resting liquidity. Queue position, hidden liquidity and
    replenishment are intentionally not inferred.
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
        any(not math.isfinite(float(value)) for value in numeric)
        or quantity <= 0
        or any(value < 0 for value in numeric[1:])
    ):
        raise ValueError("quantity and cost inputs must be finite and non-negative")
    if funding_interval_hours <= 0 or slippage_bps >= 10000:
        raise ValueError("funding interval must be positive and slippage below 10000 bps")
    if submitted_at_ms < 0 or latency_ms < 0:
        raise ValueError("timestamps and latency must be non-negative")
    ordered = tuple(snapshots)
    if any(not isinstance(snapshot, DepthBookSnapshot) for snapshot in ordered):
        raise TypeError("L2 replay requires DepthBookSnapshot records")
    if any(
        ordered[index].timestamp_ms >= ordered[index + 1].timestamp_ms
        for index in range(len(ordered) - 1)
    ):
        raise ValueError("depth snapshots must be strictly chronological")

    eligible_at = submitted_at_ms + latency_ms
    quote = next(
        (snapshot for snapshot in ordered if snapshot.timestamp_ms >= eligible_at),
        None,
    )
    remaining = float(quantity)
    fills: list[ReplayFill] = []
    fees = slippage = spread = market_impact = 0.0
    if quote is not None:
        levels = quote.asks if direction == "BUY" else quote.bids
        best_price = levels[0].price
        mid = (quote.bids[0].price + quote.asks[0].price) / 2.0
        spread_per_unit = abs(best_price - mid)
        for level in levels:
            if remaining <= 1e-12:
                break
            fill_quantity = min(remaining, level.quantity)
            if fill_quantity <= 0:
                continue
            fill_price = (
                level.price * (1.0 + slippage_bps / 10000.0)
                if direction == "BUY"
                else level.price * (1.0 - slippage_bps / 10000.0)
            )
            notional = fill_quantity * fill_price
            fee = notional * taker_fee_bps / 10000.0
            slippage_cost = fill_quantity * abs(fill_price - level.price)
            impact_per_unit = (
                level.price - best_price
                if direction == "BUY"
                else best_price - level.price
            )
            fills.append(
                ReplayFill(
                    quote.timestamp_ms,
                    fill_quantity,
                    level.price,
                    fill_price,
                    fee,
                    slippage_cost,
                )
            )
            fees += fee
            slippage += slippage_cost
            spread += fill_quantity * spread_per_unit
            market_impact += fill_quantity * impact_per_unit
            remaining -= fill_quantity

    filled = float(quantity) - remaining
    notional = sum(fill.quantity * fill.fill_price for fill in fills)
    average = notional / filled if filled > 0 else None
    funding = (
        notional * funding_bps / 10000.0 * holding_hours / funding_interval_hours
    )
    borrow = notional * borrow_bps / 10000.0 * holding_hours / 24.0
    total = fees + slippage + spread + market_impact + funding + borrow
    return ExecutionReplayResult(
        side=direction,
        requested_quantity=float(quantity),
        filled_quantity=filled,
        remaining_quantity=remaining,
        average_fill_price=average,
        fees=fees,
        slippage_cost=slippage,
        spread_cost=spread,
        funding_cost=funding,
        borrow_cost=borrow,
        total_cost=total,
        fills=tuple(fills),
        latency_ms=latency_ms,
        complete=remaining <= 1e-12,
        market_impact_cost=market_impact,
    )
