"""Canonical exchange-independent market event schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class CanonicalTrade:
    exchange: str
    market: str
    symbol: str
    trade_id: str
    timestamp: datetime
    price: float
    quantity: float
    side: Side
    is_buyer_maker: bool | None = None


@dataclass(frozen=True)
class CanonicalBookEvent:
    exchange: str
    market: str
    symbol: str
    update_id: int | str
    timestamp: datetime
    side: Side
    price: float
    quantity: float


def normalize_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
