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
    if not levels:
        raise ValueError("replay requires executable book depth")
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
    mid = reference_mid or ((bids[0].price + asks[0].price) / 2)
    if mid <= 0:
        raise ValueError("reference mid must be positive")
    impact = (
        (filled / visible) * impact_bps_per_1pct_depth * Decimal("100")
        if visible > 0
        else Decimal("0")
    )
    adverse = Decimal("1") + impact / Decimal("10000")
    adjusted_average = (
        average * adverse if side == "BUY" else average / adverse
    ) if average is not None else None
    fees_rate = maker_fee_bps if maker else taker_fee_bps
    fees = sum(
        (price * qty * fees_rate / Decimal("10000") for price, qty in raw_fills),
        Decimal("0"),
    )
    slippage = (
        ((adjusted_average - mid) if side == "BUY" else (mid - adjusted_average))
        / mid * Decimal("10000")
        if adjusted_average is not None
        else Decimal("0")
    )
    fills = tuple(
        ReplayFill(
            price=(price * adverse if side == "BUY" else price / adverse),
            quantity=qty,
            fee_quote=price * qty * fees_rate / Decimal("10000"),
            maker=maker,
        )
        for price, qty in raw_fills
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
