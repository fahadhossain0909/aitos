"""Venue-neutral contracts for the AITOS market-data plane.

The contracts intentionally separate *event time* from *ingest time* and
identify the market explicitly. This prevents stale REST recovery data from
being mistaken for live WebSocket data and makes transport failures observable.
"""

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
    """Envelope used at the boundary between transport and processing.

    ``event_time`` is the venue timestamp; ``ingest_time`` is when AITOS saw
    the message. ``source`` is never inferred from the processing path.
    """

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

    @property
    def source_age_seconds(self) -> float:
        return max(0.0, (self.ingest_time - self.event_time).total_seconds())
