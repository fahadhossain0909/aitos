"""Canonical contracts shared by every AITOS asset class."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aitos.core.immutable import freeze_value


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
    LIQUIDATION = "liquidation"
    OPTIONS = "options"


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
    market_type: str | None = None

    @property
    def id(self) -> str:
        parts = [self.venue, self.asset_class.value]
        if self.market_type:
            parts.append(self.market_type)
        parts.append(self.symbol)
        return ":".join(parts)


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
    event_revision: int = 1
    sequence: int | None = None
    trace_id: str | None = None
    venue: str | None = None
    market_type: str | None = None
    source_event_type: str | None = None
    source_instrument_id: str | None = None
    stage_timestamps: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_value(self.payload))
        object.__setattr__(
            self, "stage_timestamps", freeze_value(self.stage_timestamps)
        )
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if self.event_revision < 1:
            raise ValueError("event_revision must be positive")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.correlation_id is None:
            object.__setattr__(self, "correlation_id", self.event_id)

    @property
    def age_ms(self) -> float:
        return max(0.0, (self.received_at - self.event_time).total_seconds() * 1000.0)
