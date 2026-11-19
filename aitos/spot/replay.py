"""Deterministic Spot execution replay with depth, latency, fees and impact."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("book levels require positive price and quantity")


@dataclass(frozen=True, slots=True)
class ReplayFill:
    price: Decimal
    quantity: Decimal
    fee_quote: Decimal
    maker: bool


@dataclass(frozen=True, slots=True)
class SpotExecutionReplayResult:
    side: Literal["BUY", "SELL"]
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    gross_quote: Decimal
    fees_quote: Decimal
    market_impact_bps: Decimal
    slippage_bps: Decimal
    latency_ms: int
    fills: tuple[ReplayFill, ...]

    @property
    def unfilled_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity

    @property
    def complete(self) -> bool:
        return self.unfilled_quantity == 0


def replay_spot_market_order(
    *,
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    bids: Sequence[BookLevel],
    asks: Sequence[BookLevel],
    maker_fee_bps: Decimal,
    taker_fee_bps: Decimal,
    latency_ms: int = 0,
    impact_bps_per_1pct_depth: Decimal = Decimal("0"),
    reference_mid: Decimal | None = None,
    maker: bool = False,
) -> SpotExecutionReplayResult:
    """Walk executable depth; missing depth remains unfilled, never invented.

    latency_ms is recorded and can select a later timestamped book snapshot
    upstream. This pure function does not shift prices by guessing future data.
    """
    if maker:
        raise ValueError(
            "maker fills require queue-aware limit-order replay; use replay_spot_limit_order"
        )
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity <= 0 or latency_ms < 0:
        raise ValueError("quantity must be positive and latency non-negative")
    if min(maker_fee_bps, taker_fee_bps, impact_bps_per_1pct_depth) < 0:
        raise ValueError("fees and impact assumptions cannot be negative")
    levels = sorted(
        asks if side == "BUY" else bids,
        key=lambda x: x.price,
        reverse=side == "SELL",
    )
    if not levels or not bids or not asks:
        raise ValueError("replay requires both sides of executable book depth")
    best_bid = max(level.price for level in bids)
    best_ask = min(level.price for level in asks)
    visible = sum((level.quantity for level in levels), Decimal("0"))
    remaining = quantity
    raw_fills: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        take = min(remaining, level.quantity)
        if take > 0:
            raw_fills.append((level.price, take))
            remaining -= take
        if remaining == 0:
            break
    filled = quantity - remaining
    gross = sum((price * qty for price, qty in raw_fills), Decimal("0"))
    average = gross / filled if filled else None
    mid = reference_mid or ((best_bid + best_ask) / 2)
    if mid <= 0 or best_bid > best_ask:
        raise ValueError("reference book is crossed or mid is non-positive")
    impact = (
        (filled / visible) * impact_bps_per_1pct_depth * Decimal("100")
        if visible > 0
        else Decimal("0")
    )
    adverse = Decimal("1") + impact / Decimal("10000")
    adjusted_average = (
        average * adverse if side == "BUY" else average / adverse
    ) if average is not None else None
    fees_rate = taker_fee_bps
    fills = tuple(
        ReplayFill(
            price=(price * adverse if side == "BUY" else price / adverse),
            quantity=qty,
            fee_quote=(
                (price * adverse if side == "BUY" else price / adverse)
                * qty * fees_rate / Decimal("10000")
            ),
            maker=False,
        )
        for price, qty in raw_fills
    )
    gross = sum((fill.price * fill.quantity for fill in fills), Decimal("0"))
    fees = sum((fill.fee_quote for fill in fills), Decimal("0"))
    slippage = (
        ((adjusted_average - mid) if side == "BUY" else (mid - adjusted_average))
        / mid * Decimal("10000")
        if adjusted_average is not None
        else Decimal("0")
    )
    return SpotExecutionReplayResult(
        side=side,
        requested_quantity=quantity,
        filled_quantity=filled,
        average_price=adjusted_average,
        gross_quote=gross,
        fees_quote=fees,
        market_impact_bps=impact,
        slippage_bps=slippage,
        latency_ms=latency_ms,
        fills=fills,
    )


def replay_spot_limit_order(
    *,
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    limit_price: Decimal,
    queue_ahead: Decimal,
    traded_quantity_at_price: Decimal,
    maker_fee_bps: Decimal,
    latency_ms: int = 0,
) -> SpotExecutionReplayResult:
    """Replay passive limit fills from observed traded volume and queue ahead."""
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if min(quantity, limit_price) <= 0 or min(queue_ahead, traded_quantity_at_price) < 0:
        raise ValueError("limit replay quantities/prices are invalid")
    if maker_fee_bps < 0 or latency_ms < 0:
        raise ValueError("maker fee and latency must be non-negative")
    filled = min(quantity, max(Decimal("0"), traded_quantity_at_price - queue_ahead))
    gross = filled * limit_price
    fee = gross * maker_fee_bps / Decimal("10000")
    return SpotExecutionReplayResult(
        side=side,
        requested_quantity=quantity,
        filled_quantity=filled,
        average_price=limit_price if filled else None,
        gross_quote=gross,
        fees_quote=fee,
        market_impact_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        latency_ms=latency_ms,
        fills=(
            (ReplayFill(limit_price, filled, fee, True),) if filled else ()
        ),
    )



@dataclass(frozen=True, slots=True)
class TimedBookSnapshot:
    timestamp_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


def replay_spot_order_with_latency(
    *,
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    submitted_at_ms: int,
    latency_ms: int,
    snapshots: Sequence[TimedBookSnapshot],
    maker_fee_bps: Decimal,
    taker_fee_bps: Decimal,
    impact_bps_per_1pct_depth: Decimal = Decimal("0"),
) -> SpotExecutionReplayResult:
    """Select the first historical book available after modeled order latency."""
    if submitted_at_ms < 0 or latency_ms < 0:
        raise ValueError("submission time and latency must be non-negative")
    eligible = [
        snapshot for snapshot in snapshots
        if snapshot.timestamp_ms >= submitted_at_ms + latency_ms
    ]
    if not eligible:
        raise ValueError("no historical order-book snapshot covers modeled latency")
    selected = min(eligible, key=lambda snapshot: snapshot.timestamp_ms)
    return replay_spot_market_order(
        side=side,
        quantity=quantity,
        bids=selected.bids,
        asks=selected.asks,
        maker_fee_bps=maker_fee_bps,
        taker_fee_bps=taker_fee_bps,
        latency_ms=latency_ms,
        impact_bps_per_1pct_depth=impact_bps_per_1pct_depth,
    )
