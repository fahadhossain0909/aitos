"""Canonical market-data architecture for AITOS."""

from .adapter import CanonicalMarketDataAdapter
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
from .venues import (
    DEFAULT_VENUES,
    InstrumentKey,
    MarketType,
    Venue,
    VenueCapabilities,
    VenueConfig,
)

__all__ = [
    "DEFAULT_VENUES",
    "BookLevel",
    "CanonicalMarketDataAdapter",
    "InstrumentKey",
    "MarketDataBus",
    "MarketEvent",
    "MarketEventType",
    "MarketSource",
    "MarketType",
    "OrderBookDelta",
    "OrderBookSnapshot",
    "TradeEvent",
    "Venue",
    "VenueCapabilities",
    "VenueConfig",
    "channel_for",
    "market_event_from_wire",
    "market_event_to_wire",
]
