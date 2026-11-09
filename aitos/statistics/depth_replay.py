"""L2 market-order and passive-queue execution replay.

The caller supplies timestamped depth and queue events. Impact coefficients
must be calibrated from historical fills; defaults are zero and are not a
claim that market impact is absent.
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
        if self.timestamp_ms < 0:
            raise ValueError("timestamp must be non-negative")
        for side, levels in (("bid", self.bids), ("ask", self.asks)):
            previous = math.inf if side == "bid" else -math.inf
            for price, size in levels:
                if (
                    not math.isfinite(price)
                    or not math.isfinite(size)
                    or price <= 0
                    or size < 0
                ):
                    raise ValueError(
                        "depth levels require positive prices and non-negative finite sizes"
                    )
                if (side == "bid" and price >= previous) or (
                    side == "ask" and price <= previous
                ):
                    raise ValueError("depth levels must be strictly price sorted")
                previous = price
        if self.bids and self.asks and self.bids[0][0] > self.asks[0][0]:
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
) -> DepthReplayResult:
    """Walk visible L2 levels, respecting latency, participation and impact.

    The impact model is an explicit square-root participation curve:
    impact_bps = coefficient * sqrt(cumulative_filled / visible_side_depth).
    It is a configurable scenario model and should be calibrated per venue,
    symbol, volatility and order type before being used for forecasts.
    """
    direction = side.upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    values = (quantity, fee_bps, impact_coefficient_bps, max_participation)
    if (
        any(not math.isfinite(float(x)) for x in values)
        or quantity <= 0
        or fee_bps < 0
        or impact_coefficient_bps < 0
        or not 0 < max_participation <= 1
    ):
        raise ValueError("invalid replay quantity, fees, impact or participation")
    if submitted_at_ms < 0 or latency_ms < 0:
        raise ValueError("timestamps and latency must be non-negative")
    books = tuple(snapshots)
    if any(a.timestamp_ms >= b.timestamp_ms for a, b in zip(books, books[1:])):
        raise ValueError("depth snapshots must be strictly chronological")
    eligible = submitted_at_ms + latency_ms
    remaining = float(quantity)
    fills = []
    fees = spread = impact_total = notional = filled = 0.0
    for book in books:
        if book.timestamp_ms < eligible or remaining <= 1e-12:
            continue
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
            if impacted <= 0:
                raise ValueError("impact parameters imply a non-positive fill price")
            fee = qty * impacted * fee_bps / 10000.0
            fees += fee
            spread += qty * abs(price - mid)
            impact_total += qty * abs(impacted - price)
            notional += qty * impacted
            filled += qty
            remaining -= qty
            allowed -= qty
            fills.append(DepthFill(book.timestamp_ms, impacted, qty, impact_bps, fee))
    avg = notional / filled if filled else None
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
