"""Venue-neutral contracts for the AITOS market-data plane."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MarketSource(str, Enum):
    WEBSOCKET = "websocket"
    REST = "rest"
    REPLAY = "replay"


class MarketEventType(str, Enum):
    TRADE = "trade"
    BOOK_DELTA = "book_delta"
    BOOK_SNAPSHOT = "book_snapshot"
    TICKER = "ticker"
    KLINE = "kline"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION = "liquidation"
    OPTIONS = "options"
    INSTRUMENT = "instrument"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class TradeEvent:
    exchange: str
    market: str
    symbol: str
    trade_id: int
    price: float
    quantity: float
    event_time: datetime
    source: MarketSource = MarketSource.WEBSOCKET
    ingest_time: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    exchange: str
    market: str
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    last_update_id: int
    event_time: datetime
    source: MarketSource = MarketSource.WEBSOCKET
    ingest_time: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class OrderBookDelta:
    exchange: str
    market: str
    symbol: str
    first_update_id: int
    final_update_id: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    event_time: datetime
    source: MarketSource = MarketSource.WEBSOCKET
    ingest_time: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """Canonical envelope shared by every market-data adapter."""

    event_type: MarketEventType
    exchange: str
    market: str
    symbol: str
    event_time: datetime
    payload: dict[str, Any]
    source: MarketSource
    ingest_time: datetime = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1
    venue: str | None = None
    market_type: str | None = None
    instrument_id: str | None = None
    sequence: int | None = None
    correlation_id: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.venue is None:
            object.__setattr__(self, "venue", self.exchange)
        if self.market_type is None:
            object.__setattr__(self, "market_type", self.market)
        if self.instrument_id is None:
            object.__setattr__(self, "instrument_id", self.symbol)
        if self.correlation_id is None:
            object.__setattr__(self, "correlation_id", self.event_id)
        if self.trace_id is None:
            object.__setattr__(self, "trace_id", self.event_id)

    @property
    def source_age_seconds(self) -> float:
        return max(0.0, (self.ingest_time - self.event_time).total_seconds())

    @property
    def source_ts(self) -> datetime:
        return self.event_time

    @property
    def received_ts(self) -> datetime:
        return self.ingest_time

    def __len__(self) -> int:
        """Expose a singleton sequence view for legacy parser callers."""
        return 1

    def __getitem__(self, index: int) -> MarketEvent:
        """Return this event at index zero for legacy parser compatibility."""
        if index in (0, -1):
            return self
        raise IndexError(index)
