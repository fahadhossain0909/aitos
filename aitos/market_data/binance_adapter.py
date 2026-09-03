"""Canonical Binance market-data adapter facade.

The existing exchange adapter owns the actual socket/REST implementation. This
facade converts its normalized legacy models into the canonical MarketEvent
contract, keeping transport details out of consumers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

from aitos.exchange.base import ExchangeAdapter

from .contracts import MarketEvent, MarketSource
from .legacy_bridge import book_snapshot_event, trade_event
from .venues import MarketType, Venue, VenueCapabilities


class BinanceCanonicalMarketDataAdapter:
    """Normalize Binance exchange streams into the canonical event contract."""

    def __init__(
        self, exchange: ExchangeAdapter, market_type: str = "usd_m_futures"
    ) -> None:
        self.exchange = exchange
        self._market_type = MarketType(market_type)

    @property
    def venue(self) -> Venue:
        return Venue.BINANCE

    @property
    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            funding=True,
            open_interest=True,
            liquidations=True,
            options=True,
        )

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        async for trade in self.exchange.stream_trades(symbols):
            event = trade_event(
                trade,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )
            yield event

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[MarketEvent]:
        async for book in self.exchange.stream_order_book(symbols, levels=levels):
            event = book_snapshot_event(
                book,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )
            yield event

    async def recover_trade_events(
        self, symbol: str, limit: int = 500
    ) -> list[MarketEvent]:
        trades = await self.exchange.fetch_recent_trades(symbol, limit=limit)
        return [
            trade_event(t, market_type=self.market_type, source=MarketSource.REST)
            for t in trades
        ]

    async def recover_order_book(self, symbol: str, levels: int = 50) -> MarketEvent:
        book = await self.exchange.fetch_order_book(symbol, limit=levels)
        return book_snapshot_event(
            book,
            market_type=self.market_type,
            source=MarketSource.REST,
        )

    @staticmethod
    def age_seconds(event: MarketEvent) -> float:
        return max(0.0, (datetime.now(timezone.utc) - event.event_time).total_seconds())
