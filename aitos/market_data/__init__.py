"""Canonical market-data architecture for AITOS."""

from .bus import (
    MarketDataBus,
    channel_for,
    market_event_from_wire,
    market_event_to_wire,
)
from .contracts import (
    BookLevel,
    MarketEvent,
    MarketEventType,
    MarketSource,
    OrderBookDelta,
    OrderBookSnapshot,
    TradeEvent,
)

__all__ = [
    "BookLevel",
    "MarketDataBus",
    "MarketEvent",
    "MarketEventType",
    "MarketSource",
    "OrderBookDelta",
    "OrderBookSnapshot",
    "TradeEvent",
    "channel_for",
    "market_event_from_wire",
    "market_event_to_wire",
]
