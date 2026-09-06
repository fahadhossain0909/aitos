"""Canonical contracts shared by every AITOS asset class."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    FOREX = "forex"
    FUTURES = "futures"
    COMMODITY = "commodity"
    RATES = "rates"
    BOND = "bond"
    OPTION = "option"
    INDEX = "index"


class MarketEventType(str, Enum):
    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"
    ORDER_BOOK = "order_book"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    VOLATILITY = "volatility"
    MACRO = "macro"
    NEWS = "news"
    ON_CHAIN = "on_chain"
    INSTRUMENT = "instrument"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Instrument:
    """Universal tradable/research instrument identity.

    Exchange/broker details remain metadata; strategies consume this stable
    identity and never call a venue API directly.
    """

    symbol: str
    asset_class: AssetClass
    venue: str
    currency: str = "USD"
    tick_size: float | None = None
    lot_size: float | None = None
    contract_size: float | None = None
    trading_hours: str = "24/7"
    settlement_type: str = "spot"
    margin_model: str = "cash"
    underlying: str | None = None

    @property
    def id(self) -> str:
        return f"{self.venue}:{self.asset_class.value}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """Asset-class-neutral event envelope used by intelligence and replay."""

    event_type: MarketEventType
    instrument: Instrument
    event_time: datetime
    payload: dict[str, Any]
    source: str
    received_at: datetime = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if self.correlation_id is None:
            object.__setattr__(self, "correlation_id", self.event_id)

    @property
    def age_ms(self) -> float:
        return max(0.0, (self.received_at - self.event_time).total_seconds() * 1000.0)
