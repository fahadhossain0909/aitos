"""Event-driven live market cache for the OpportunityScanner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.models.market import OrderBookSnapshot, TradeTick


@dataclass
class LiveSymbolCache:
    trades: deque = field(default_factory=deque)
    order_book: Optional[OrderBookSnapshot] = None
    last_trade_at: Optional[datetime] = None
    last_book_at: Optional[datetime] = None
    liquidity_events: deque = field(default_factory=lambda: deque(maxlen=200))


class LiveScannerCache:
    """Consumes canonical EventBus market events and keeps a live view."""

    def __init__(
        self, event_bus: EventBus, symbols: list[str], max_trades: int = 5000
    ) -> None:
        self._bus = event_bus
        self._symbols = set(symbols)
        self._max_trades = max(100, max_trades)
        self._state: Dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._initialized = False

    def _cache(self, symbol: str) -> LiveSymbolCache:
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    async def initialize(self) -> None:
        if self._initialized:
            return
        for symbol in self._symbols:
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.trade.{symbol}",
                    self._on_trade,
                    group="live-scanner-trades",
                )
            )
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.orderbook.{symbol}",
                    self._on_book,
                    group="live-scanner-book",
                )
            )
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.liquidity.{symbol}",
                    self._on_liquidity,
                    group="live-scanner-liquidity",
                )
            )
        self._initialized = True

    async def shutdown(self) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def _on_trade(self, event: Event) -> None:
        trade = TradeTick.from_dict(event.payload)
        state = self._cache(trade.symbol)
        state.trades.append(trade)
        state.last_trade_at = trade.timestamp

    async def _on_book(self, event: Event) -> None:
        book = OrderBookSnapshot.from_dict(event.payload)
        state = self._cache(book.symbol)
        state.order_book = book
        state.last_book_at = book.timestamp

    async def _on_liquidity(self, event: Event) -> None:
        payload = dict(event.payload)
        symbol = payload.get("symbol") or event.topic.rsplit(".", 1)[-1]
        self._cache(symbol).liquidity_events.append(payload)

    def snapshot(self, symbol: str) -> Optional[LiveSymbolCache]:
        return self._state.get(symbol)

    def recent_trades(
        self, symbol: str, limit: Optional[int] = None
    ) -> list[TradeTick]:
        state = self._state.get(symbol)
        trades = list(state.trades) if state else []
        return trades[-limit:] if limit else trades

    def order_book(self, symbol: str) -> Optional[OrderBookSnapshot]:
        state = self._state.get(symbol)
        return state.order_book if state else None

    def liquidity_events(self, symbol: str) -> list[dict]:
        state = self._state.get(symbol)
        return list(state.liquidity_events) if state else []
