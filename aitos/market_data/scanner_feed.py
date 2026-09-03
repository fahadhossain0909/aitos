"""Canonical market-data consumer for scanner live state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.models.market import OrderBookSnapshot, TradeTick

from .bus import MarketDataBus
from .contracts import MarketEvent, MarketEventType

TradeHandler = Callable[[TradeTick], Awaitable[None]]
BookHandler = Callable[[OrderBookSnapshot], Awaitable[None]]


class CanonicalScannerFeed:
    """Feeds scanner callbacks exclusively from canonical semantic channels."""

    def __init__(self, event_bus: EventBus, market_bus: MarketDataBus) -> None:
        self._event_bus = event_bus
        self._market_bus = market_bus
        self._subscriptions: list[Subscription] = []
        self._trade_handler: TradeHandler | None = None
        self._book_handler: BookHandler | None = None
        self._last_event_at: datetime | None = None

    async def start(
        self, trade_handler: TradeHandler, book_handler: BookHandler
    ) -> None:
        if self._subscriptions:
            return
        self._trade_handler = trade_handler
        self._book_handler = book_handler
        self._subscriptions = [
            await self._market_bus.subscribe(
                MarketEventType.TRADE,
                self._on_trade,
                group="market-scanner",
                live_only=True,
            ),
            await self._market_bus.subscribe(
                MarketEventType.BOOK_SNAPSHOT,
                self._on_book,
                group="market-scanner",
                live_only=True,
            ),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        if self._subscriptions:
            await asyncio.gather(
                *(self._wait(subscription) for subscription in self._subscriptions),
                return_exceptions=True,
            )
        self._subscriptions.clear()

    async def _wait(self, subscription: Subscription) -> None:
        try:
            await subscription._task
        except asyncio.CancelledError:
            return

    async def _on_trade(self, event: MarketEvent) -> None:
        if self._trade_handler is None:
            return
        payload = dict(event.payload)
        payload.pop("_market_source", None)
        await self._trade_handler(TradeTick.from_dict(payload))
        self._last_event_at = datetime.now(timezone.utc)

    async def _on_book(self, event: MarketEvent) -> None:
        if self._book_handler is None:
            return
        payload = dict(event.payload)
        payload.pop("_market_source", None)
        payload["symbol"] = event.symbol
        payload["timestamp"] = event.event_time.isoformat()
        await self._book_handler(OrderBookSnapshot.from_dict(payload))
        self._last_event_at = datetime.now(timezone.utc)

    @property
    def last_event_at(self) -> datetime | None:
        return self._last_event_at
