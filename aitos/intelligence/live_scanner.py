"""Event-driven live market cache for the OpportunityScanner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    last_trade_received_at: Optional[datetime] = None
    last_book_received_at: Optional[datetime] = None
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
        state.last_trade_received_at = datetime.now(timezone.utc)

    async def _on_book(self, event: Event) -> None:
        book = OrderBookSnapshot.from_dict(event.payload)
        state = self._cache(book.symbol)
        state.order_book = book
        state.last_book_at = book.timestamp
        state.last_book_received_at = datetime.now(timezone.utc)

    async def _on_liquidity(self, event: Event) -> None:
        payload = dict(event.payload)
        symbol = payload.get("symbol") or event.topic.rsplit(".", 1)[-1]
        self._cache(symbol).liquidity_events.append(payload)

    def snapshot(self, symbol: str) -> Optional[LiveSymbolCache]:
        return self._state.get(symbol)

    @staticmethod
    def _age_seconds(timestamp: Optional[datetime], now: datetime) -> Optional[float]:
        if timestamp is None:
            return None
        return max(0.0, (now - timestamp).total_seconds())

    def freshness_snapshot(self, symbol: str) -> dict:
        """Expose source age and consumer lag without changing freshness semantics."""
        state = self._state.get(symbol)
        if state is None:
            return {
                "cache_has_state": False,
                "last_trade_at": None,
                "last_book_at": None,
                "last_trade_received_at": None,
                "last_book_received_at": None,
                "trade_age_sec": None,
                "book_age_sec": None,
                "trade_consumer_lag_sec": None,
                "book_consumer_lag_sec": None,
            }

        now = datetime.now(timezone.utc)
        trade_age = self._age_seconds(state.last_trade_at, now)
        book_age = self._age_seconds(state.last_book_at, now)
        trade_received_age = self._age_seconds(state.last_trade_received_at, now)
        book_received_age = self._age_seconds(state.last_book_received_at, now)
        return {
            "cache_has_state": True,
            "last_trade_at": (
                state.last_trade_at.isoformat() if state.last_trade_at else None
            ),
            "last_book_at": (
                state.last_book_at.isoformat() if state.last_book_at else None
            ),
            "last_trade_received_at": (
                state.last_trade_received_at.isoformat()
                if state.last_trade_received_at
                else None
            ),
            "last_book_received_at": (
                state.last_book_received_at.isoformat()
                if state.last_book_received_at
                else None
            ),
            "trade_age_sec": round(trade_age, 3) if trade_age is not None else None,
            "book_age_sec": round(book_age, 3) if book_age is not None else None,
            "trade_consumer_lag_sec": (
                round(max(0.0, trade_age - trade_received_age), 3)
                if trade_age is not None and trade_received_age is not None
                else None
            ),
            "book_consumer_lag_sec": (
                round(max(0.0, book_age - book_received_age), 3)
                if book_age is not None and book_received_age is not None
                else None
            ),
        }

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
