"""Injectable point-in-time valuation; never uses future prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PriceQuote:
    asset_id: str
    quote_currency: str
    price: float
    observed_at: datetime
    source: str

    def __post_init__(self):
        if not self.asset_id or not self.quote_currency or not self.source:
            raise ValueError("quote provenance required")
        if not isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be positive and finite")
        if self.observed_at.tzinfo is None:
            raise ValueError("quote timestamp must be timezone-aware")


class PointInTimePriceBook(Protocol):
    async def quote(self, asset_id: str, at: datetime) -> PriceQuote | None: ...


async def value_quantity(
    price_book: PointInTimePriceBook,
    asset_id: str,
    quantity: float,
    *,
    at: datetime,
    max_age_seconds: float = 30.0,
) -> tuple[float, PriceQuote] | None:
    if at.tzinfo is None or not isfinite(quantity) or quantity < 0:
        raise ValueError("invalid valuation input")
    quote = await price_book.quote(asset_id, at.astimezone(timezone.utc))
    if quote is None:
        return None
    age = (
        at.astimezone(timezone.utc) - quote.observed_at.astimezone(timezone.utc)
    ).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None
    value = quantity * quote.price
    if not isfinite(value):
        return None
    return value, quote
