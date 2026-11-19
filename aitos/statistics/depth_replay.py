"""L2 market-order and passive-queue execution replay.

The caller supplies timestamped depth and queue events. Market orders consume
one point-in-time book after modeled latency and retain any unfilled remainder.
Fees, spread, calibrated impact, additional slippage, funding and borrow costs
are reported separately. Impact coefficients must be calibrated from historical
fills; defaults are zero and are not a claim that market impact is absent.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class DepthBookSnapshot:
    timestamp_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("timestamp must be a non-negative integer")
        for side in ("bids", "asks"):
            raw_levels = tuple(getattr(self, side))
            if not raw_levels:
                raise ValueError("depth snapshot requires both book sides")
            normalized = []
            previous = math.inf if side == "bids" else -math.inf
            for level in raw_levels:
                if not isinstance(level, (tuple, list)) or len(level) != 2:
                    raise ValueError("depth levels must be price/size pairs")
                raw_price, raw_size = level
                if isinstance(raw_price, bool) or isinstance(raw_size, bool):
                    raise ValueError("depth prices and sizes must be numeric")
                try:
                    price, size = float(raw_price), float(raw_size)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("depth prices and sizes must be numeric") from None
                if (
                    not math.isfinite(price)
                    or not math.isfinite(size)
                    or price <= 0
                    or size < 0
                ):
                    raise ValueError(
                        "depth levels require positive prices and non-negative finite sizes"
                    )
                if (side == "bids" and price >= previous) or (
                    side == "asks" and price <= previous
                ):
                    raise ValueError("depth levels must be strictly price sorted")
                previous = price
                normalized.append((price, size))
            object.__setattr__(self, side, tuple(normalized))
        if self.bids[0][0] > self.asks[0][0]:
            raise ValueError("crossed order book")


@dataclass(frozen=True)
class DepthFill:
    timestamp_ms: int
    price: float
    quantity: float
    impact_bps: float
    fee: float


@dataclass(frozen=True)
class DepthReplayResult:
    side: str
    requested_quantity: float
    filled_quantity: float
    remaining_quantity: float
    average_fill_price: float | None
    fees: float
    spread_cost: float
    impact_cost: float
    fills: tuple[DepthFill, ...]
    complete: bool
    slippage_cost: float = 0.0
    funding_cost: float = 0.0
    borrow_cost: float = 0.0
    total_cost: float = 0.0


def replay_l2_market_order(
    snapshots: Iterable[DepthBookSnapshot],
    *,
    side: str,
    quantity: float,
    submitted_at_ms: int,
    latency_ms: int,
    fee_bps: float,
    impact_coefficient_bps: float = 0.0,
    max_participation: float = 1.0,
    slippage_bps: float = 0.0,
    funding_bps: float = 0.0,
    borrow_bps: float = 0.0,
    holding_hours: float = 0.0,
    funding_interval_hours: float = 8.0,
) -> DepthReplayResult:
    """Walk visible L2 levels, respecting latency, participation and impact.

    The impact model is an explicit square-root participation curve:
    impact_bps = coefficient * sqrt(cumulative_filled / visible_side_depth).
    It is a configurable scenario model and should be calibrated per venue,
    symbol, volatility and order type before being used for forecasts.
    """
    if not isinstance(side, str):
        raise ValueError("side must be BUY or SELL")
    direction = side.upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    values = (
        quantity,
        fee_bps,
        impact_coefficient_bps,
        max_participation,
        slippage_bps,
        funding_bps,
        borrow_bps,
        holding_hours,
        funding_interval_hours,
    )
    if (
        any(not math.isfinite(float(value)) for value in values)
        or quantity <= 0
        or any(value < 0 for value in values[1:])
        or not 0 < max_participation <= 1
        or funding_interval_hours <= 0
        or slippage_bps >= 10000
    ):
        raise ValueError("invalid replay quantity, cost, participation or horizon")
    if (
        isinstance(submitted_at_ms, bool)
        or isinstance(latency_ms, bool)
        or submitted_at_ms < 0
        or latency_ms < 0
    ):
        raise ValueError("timestamps and latency must be non-negative")
    books = tuple(snapshots)
    if any(not isinstance(book, DepthBookSnapshot) for book in books):
        raise TypeError("L2 replay requires DepthBookSnapshot records")
    if any(a.timestamp_ms >= b.timestamp_ms for a, b in zip(books, books[1:])):
        raise ValueError("depth snapshots must be strictly chronological")
    eligible = submitted_at_ms + latency_ms
    remaining = float(quantity)
    fills = []
    fees = spread = impact_total = slippage_total = notional = filled = 0.0
    for book in books:
        if book.timestamp_ms < eligible:
            continue
        if remaining <= 1e-12:
            break
        levels = book.asks if direction == "BUY" else book.bids
        opposite = book.bids if direction == "BUY" else book.asks
        if not levels or not opposite:
            continue
        visible = sum(size for _, size in levels)
        if visible <= 0:
            continue
        mid = (book.bids[0][0] + book.asks[0][0]) / 2.0
        allowed = visible * max_participation
        for price, size in levels:
            if remaining <= 1e-12 or allowed <= 1e-12:
                break
            qty = min(remaining, size, allowed)
            if qty <= 0:
                continue
            filled_after = filled + qty
            participation = min(1.0, filled_after / max(visible, 1e-12))
            impact_bps = impact_coefficient_bps * math.sqrt(participation)
            impacted = price * (
                1.0 + impact_bps / 10000.0
                if direction == "BUY"
                else 1.0 - impact_bps / 10000.0
            )
            fill_price = impacted * (
                1.0 + slippage_bps / 10000.0
                if direction == "BUY"
                else 1.0 - slippage_bps / 10000.0
            )
            if fill_price <= 0:
                raise ValueError("impact and slippage imply a non-positive fill price")
            fee = qty * fill_price * fee_bps / 10000.0
            fees += fee
            spread += qty * abs(levels[0][0] - mid)
            impact_total += qty * abs(impacted - levels[0][0])
            slippage_total += qty * abs(fill_price - impacted)
            notional += qty * fill_price
            filled += qty
            remaining -= qty
            allowed -= qty
            fills.append(
                DepthFill(book.timestamp_ms, fill_price, qty, impact_bps, fee)
            )
        # A market order consumes one point-in-time book only. Reusing later
        # snapshots would fabricate resting-order queue priority/replenishment.
        break
    avg = notional / filled if filled else None
    funding = (
        notional * funding_bps / 10000.0 * holding_hours / funding_interval_hours
    )
    borrow = notional * borrow_bps / 10000.0 * holding_hours / 24.0
    total_cost = fees + spread + impact_total + slippage_total + funding + borrow
    return DepthReplayResult(
        direction,
        quantity,
        filled,
        remaining,
        avg,
        fees,
        spread,
        impact_total,
        tuple(fills),
        remaining <= 1e-12,
        slippage_total,
        funding,
        borrow,
        total_cost,
    )


@dataclass(frozen=True)
class LimitQueueEvent:
    timestamp_ms: int
    traded_at_limit: float = 0.0
    cancelled_ahead: float = 0.0
    added_ahead: float = 0.0


@dataclass(frozen=True)
class QueueReplayResult:
    requested_quantity: float
    filled_quantity: float
    remaining_quantity: float
    initial_queue_ahead: float
    final_queue_ahead: float
    fills: tuple[tuple[int, float], ...]
    complete: bool


def replay_passive_limit_queue(
    events: Iterable[LimitQueueEvent],
    *,
    quantity: float,
    queue_ahead: float,
    submitted_at_ms: int,
) -> QueueReplayResult:
    """Replay queue priority from observed trades/cancels/additions ahead.

    Cancel/add events must be classified as ahead of the simulated order by
    the source adapter. Unclassified aggregate depth changes are insufficient
    to infer queue position and must not be passed as queue-ahead events.
    """
    if (
        not math.isfinite(quantity)
        or not math.isfinite(queue_ahead)
        or quantity <= 0
        or queue_ahead < 0
        or submitted_at_ms < 0
    ):
        raise ValueError("invalid simulated order or queue size")
    ordered = tuple(events)
    if any(a.timestamp_ms >= b.timestamp_ms for a, b in zip(ordered, ordered[1:])):
        raise ValueError("queue events must be strictly chronological")
    ahead = float(queue_ahead)
    remaining = float(quantity)
    fills = []
    for event in ordered:
        if event.timestamp_ms < submitted_at_ms:
            raise ValueError("queue event predates simulated order submission")
        values = (event.traded_at_limit, event.cancelled_ahead, event.added_ahead)
        if any(not math.isfinite(x) or x < 0 for x in values):
            raise ValueError("queue event quantities must be finite and non-negative")
        ahead = max(0.0, ahead - event.cancelled_ahead) + event.added_ahead
        traded = event.traded_at_limit
        consumed = min(ahead, traded)
        ahead -= consumed
        own_fill = min(remaining, max(0.0, traded - consumed))
        if own_fill > 0:
            fills.append((event.timestamp_ms, own_fill))
            remaining -= own_fill
        if remaining <= 1e-12:
            break
    filled = quantity - remaining
    return QueueReplayResult(
        quantity,
        filled,
        remaining,
        queue_ahead,
        ahead,
        tuple(fills),
        remaining <= 1e-12,
    )
