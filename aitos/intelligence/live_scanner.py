"""Event-driven live market cache for the OpportunityScanner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.intelligence.live_bridge import (
    register_book_handler,
    register_trade_handler,
    unregister_book_handler,
    unregister_trade_handler,
)
from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot, TradeTick

logger = get_logger("aitos.intelligence.live_scanner")

LIVE_LIQUIDITY_GROUP = "live-scanner-liquidity-v2"
LIVE_TRADE_MAX_AGE_SECONDS = 15.0


@dataclass
class LiveSymbolCache:
    trades: deque = field(default_factory=deque)
    order_book: OrderBookSnapshot | None = None
    last_trade_at: datetime | None = None
    last_book_at: datetime | None = None
    last_trade_received_at: datetime | None = None
    last_book_received_at: datetime | None = None
    liquidity_events: deque = field(default_factory=lambda: deque(maxlen=200))


class LiveScannerCache:
    """Maintains live state with a process-local WebSocket fast lane."""

    def __init__(
        self, event_bus: EventBus, symbols: list[str], max_trades: int = 5000
    ) -> None:
        self._bus = event_bus
        self._symbols = set(symbols)
        self._max_trades = max(100, max_trades)
        self._state: dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._direct_symbols: set[str] = set()
        self._initialized = False
        self._last_freshness_log_at: dict[str, datetime] = {}

    def _cache(self, symbol: str) -> LiveSymbolCache:
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    async def initialize(self) -> None:
        if self._initialized:
            return
        for symbol in self._symbols:
            register_trade_handler(symbol, self._on_direct_trade)
            register_book_handler(symbol, self._on_direct_book)
            self._direct_symbols.add(symbol)
            # Liquidity is derived/non-critical; it may continue to use Redis.
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.liquidity.{symbol}",
                    self._on_liquidity,
                    group=LIVE_LIQUIDITY_GROUP,
                    start_id="$",
                )
            )
        self._initialized = True
        logger.info(
            "live scanner direct market-data fast lane enabled",
            extra={
                "aitos_extra": {
                    "symbols": sorted(self._symbols),
                    "redis_live_trade_bridge": False,
                    "redis_live_book_bridge": False,
                }
            },
        )

    async def shutdown(self) -> None:
        for symbol in tuple(self._direct_symbols):
            unregister_trade_handler(symbol, self._on_direct_trade)
            unregister_book_handler(symbol, self._on_direct_book)
        self._direct_symbols.clear()
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def _on_direct_trade(self, trade: TradeTick) -> None:
        now = datetime.now(timezone.utc)
        state = self._cache(trade.symbol)
        age = max(0.0, (now - trade.timestamp).total_seconds())
        if age > LIVE_TRADE_MAX_AGE_SECONDS:
            logger.info(
                "ignored stale trade in direct live scanner",
                extra={
                    "aitos_extra": {
                        "symbol": trade.symbol,
                        "trade_id": trade.trade_id,
                        "trade_age_sec": round(age, 3),
                        "max_age_seconds": LIVE_TRADE_MAX_AGE_SECONDS,
                        "source": "direct_websocket_fast_lane",
                    }
                },
            )
            return
        state.trades.append(trade)
        state.last_trade_at = trade.timestamp
        state.last_trade_received_at = now
        self._maybe_log_freshness(trade.symbol)

    async def _on_direct_book(self, book: OrderBookSnapshot) -> None:
        state = self._cache(book.symbol)
        state.order_book = book
        state.last_book_at = book.timestamp
        state.last_book_received_at = datetime.now(timezone.utc)
        self._maybe_log_freshness(book.symbol)

    async def _on_liquidity(self, event: Event) -> None:
        payload = dict(event.payload)
        symbol = payload.get("symbol") or event.topic.rsplit(".", 1)[-1]
        self._cache(symbol).liquidity_events.append(payload)

    def snapshot(self, symbol: str) -> LiveSymbolCache | None:
        self._maybe_log_freshness(symbol)
        return self._state.get(symbol)

    @staticmethod
    def _age_seconds(timestamp: datetime | None, now: datetime) -> float | None:
        if timestamp is None:
            return None
        return max(0.0, (now - timestamp).total_seconds())

    def freshness_snapshot(self, symbol: str) -> dict:
        """Expose source age and receive age for direct-live diagnostics."""
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
                "live_data_transport": "direct_websocket",
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
            "live_data_transport": "direct_websocket",
        }

    def _maybe_log_freshness(self, symbol: str) -> None:
        now = datetime.now(timezone.utc)
        previous = self._last_freshness_log_at.get(symbol)
        if previous is not None and (now - previous).total_seconds() < 30.0:
            return
        self._last_freshness_log_at[symbol] = now
        snapshot = self.freshness_snapshot(symbol)
        logger.info(
            "live scanner freshness",
            extra={"aitos_extra": {"symbol": symbol, **snapshot}},
        )

    def recent_trades(self, symbol: str, limit: int | None = None) -> list[TradeTick]:
        state = self._state.get(symbol)
        trades = list(state.trades) if state else []
        return trades[-limit:] if limit else trades

    def order_book(self, symbol: str) -> OrderBookSnapshot | None:
        state = self._state.get(symbol)
        return state.order_book if state else None

    def liquidity_events(self, symbol: str) -> list[dict]:
        state = self._state.get(symbol)
        return list(state.liquidity_events) if state else []
