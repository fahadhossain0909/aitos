"""Protocol shared by all canonical market-data venue adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .contracts import MarketEvent
from .venues import MarketType, Venue, VenueCapabilities


@runtime_checkable
class CanonicalMarketDataAdapter(Protocol):
    """Minimal transport contract consumed by the canonical runtime.

    Venue-specific adapters may expose additional recovery or instrument APIs,
    but the runtime only depends on these normalized streaming operations.
    """

    @property
    def venue(self) -> Venue: ...

    @property
    def market_type(self) -> MarketType: ...

    @property
    def capabilities(self) -> VenueCapabilities: ...

    def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]: ...

    def stream_order_books(
        self, symbols: list[str], levels: int
    ) -> AsyncIterator[MarketEvent]: ...
