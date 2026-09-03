"""Canonical market-data architecture for AITOS.

This package is deliberately infrastructure-neutral. Exchange adapters normalize
venue-specific messages into the contracts in :mod:`aitos.market_data.contracts`.
Storage, scanning and strategy layers consume these contracts without knowing
Binance WebSocket/REST details.
"""

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
    "MarketEvent",
    "MarketEventType",
    "MarketSource",
    "OrderBookDelta",
    "OrderBookSnapshot",
    "TradeEvent",
]
