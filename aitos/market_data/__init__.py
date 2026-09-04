"""Canonical market-data architecture for AITOS."""

from .adapter import CanonicalMarketDataAdapter
from .binance_adapter import BinanceCanonicalMarketDataAdapter
from .bybit_adapter import BybitCanonicalMarketDataAdapter
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
from .okx_adapter import OKXCanonicalMarketDataAdapter
from .registry import VenueRegistry
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
    "BinanceCanonicalMarketDataAdapter",
    "BookLevel",
    "BybitCanonicalMarketDataAdapter",
    "CanonicalMarketDataAdapter",
    "InstrumentKey",
    "MarketDataBus",
    "MarketEvent",
    "MarketEventType",
    "MarketSource",
    "MarketType",
    "OKXCanonicalMarketDataAdapter",
    "OrderBookDelta",
    "OrderBookSnapshot",
    "TradeEvent",
    "Venue",
    "VenueCapabilities",
    "VenueConfig",
    "VenueRegistry",
    "channel_for",
    "market_event_from_wire",
    "market_event_to_wire",
]
