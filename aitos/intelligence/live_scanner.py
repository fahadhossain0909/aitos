"""Event-driven live market cache for the OpportunityScanner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot, TradeTick

logger = get_logger("aitos.intelligence.live_scanner")

LIVE_TRADE_GROUP = "live-scanner-trades-v2"
LIVE_BOOK_GROUP = "live-scanner-book-v2"
LIVE_LIQUIDITY_GROUP = "live-scanner-liquidity-v2"
LIVE_TRADE_MAX_AGE_SECONDS = 15.0


@dataclass
class LiveSymbolCache:
    trades: deque = field(default_factory=deque)
    last_trade_at: datetime | None = None
    last_book_at: datetime | None = None
    last_trade_source_at: datetime | None = None
    last_book_source_at: datetime | None = None
    last_trade_received_at: datetime | None = None
    last_book_received_at: datetime | None = None
    order_book: OrderBookSnapshot | None = None
    liquidity_events: deque = field(default_factory=lambda: deque(maxlen=200))


class LiveScannerCache:
    """Consumes canonical EventBus market events and keeps a live view.

    The cache remains subscribed even when the scanner is temporarily using
    REST data.  REST fallback is a read-path decision in OpportunityScanner;
    it must never disable the live WebSocket/EventBus consumer.  This makes
    recovery automatic: as soon as fresh WS events arrive, the next scan can
    switch back to websocket_live_state without a restart or manual reset.
    """

    def __init__(
        self, event_bus: EventBus, symbols: list[str], max_trades: int = 5000
    ) -> None:
        self._bus = event_bus
        self._symbols = set(symbols)
        self._max_trades = max(100, max_trades)
        self._state: dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._direct_market_data = False
        self._last_freshness_log_at: dict[str, datetime] = {}

    def _cache(self, symbol: str) -> LiveSymbolCache:
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    async def initialize(self, direct_market_data: bool = False) -> None:
        """Start live consumers; never turn them off for REST fallback.

        ``direct_market_data`` is retained for API compatibility with older
        callers.  The canonical EventBus subscription is always established.
        Direct ingestion callbacks may also feed the cache, so handlers are
        idempotent to prevent duplicate trades/books when both paths are live.
        """
        self._direct_market_data = direct_market_data
        if self._initialized:
            return
        for symbol in self._symbols:
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.trade.{symbol}",
                    self._on_trade,
                    group=LIVE_TRADE_GROUP,
                    start_id="$",
                )
            )
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.orderbook.{symbol}",
                    self._on_book,
                    group=LIVE_BOOK_GROUP,
                    start_id="$",
                )
            )
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.liquidity.{symbol}",
                    self._on_liquidity,
                    group=LIVE_LIQUIDITY_GROUP,
                    start_id="$",
                )
            )
        self._initialized = True

    async def shutdown(self) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def accept_live_trade(self, trade: TradeTick) -> None:
        await self._on_trade(
            Event(topic=f"market.trade.{trade.symbol}", payload=trade.to_dict())
        )

    async def accept_live_order_book(self, book: OrderBookSnapshot) -> None:
        await self._on_book(
            Event(topic=f"market.orderbook.{book.symbol}", payload=book.to_dict())
        )

    async def _on_trade(self, event: Event) -> None:
        trade = TradeTick.from_dict(event.payload)
        received_at = datetime.now(timezone.utc)
        source_age = (received_at - trade.timestamp).total_seconds()
        if source_age > LIVE_TRADE_MAX_AGE_SECONDS:
            logger.warning(
                "discarded stale live trade",
                extra={
                    "aitos_extra": {
                        "symbol": trade.symbol,
                        "trade_id": trade.trade_id,
                        "source_age_sec": round(max(0.0, source_age), 3),
                        "max_source_age_seconds": LIVE_TRADE_MAX_AGE_SECONDS,
                    }
                },
            )
            return

        state = self._cache(trade.symbol)
        # Ingestion can feed the cache directly while the canonical EventBus
        # consumer receives the same event.  Treat trade IDs as idempotency
        # keys so enabling both paths cannot double-count live trades.
        if state.trades and trade.trade_id <= state.trades[-1].trade_id:
            return
        state.trades.append(trade)
        state.last_trade_at = received_at
        state.last_trade_source_at = trade.timestamp
        state.last_trade_received_at = received_at
        self._maybe_log_freshness(trade.symbol)

    async def _on_book(self, event: Event) -> None:
        book = OrderBookSnapshot.from_dict(event.payload)
        received_at = datetime.now(timezone.utc)
        state = self._cache(book.symbol)
        # Direct handler + EventBus can legitimately deliver the same snapshot.
        # update_id is the exchange sequence identity when available.
        if (
            state.order_book is not None
            and state.order_book.update_id == book.update_id
            and state.order_book.timestamp == book.timestamp
        ):
            return
        state.order_book = book
        state.last_book_at = received_at
        state.last_book_source_at = book.timestamp
        state.last_book_received_at = received_at
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
        state = self._state.get(symbol)
        if state is None:
            return {
                "cache_has_state": False,
                "last_trade_at": None,
                "last_book_at": None,
                "last_trade_source_at": None,
                "last_book_source_at": None,
                "last_trade_received_at": None,
                "last_book_received_at": None,
                "trade_age_sec": None,
                "book_age_sec": None,
                "trade_receive_age_sec": None,
                "book_receive_age_sec": None,
                "trade_source_age_sec": None,
                "book_source_age_sec": None,
                "trade_consumer_lag_sec": None,
                "book_consumer_lag_sec": None,
            }
        now = datetime.now(timezone.utc)
        trade_age = self._age_seconds(state.last_trade_at, now)
        book_age = self._age_seconds(state.last_book_at, now)
        trade_source_at = state.last_trade_source_at or state.last_trade_at
        book_source_at = state.last_book_source_at or state.last_book_at
        trade_source_age = self._age_seconds(trade_source_at, now)
        book_source_age = self._age_seconds(book_source_at, now)
        trade_receive_age = self._age_seconds(state.last_trade_received_at, now)
        book_receive_age = self._age_seconds(state.last_book_received_at, now)
        trade_lag = (
            None
            if trade_source_age is None or trade_receive_age is None
            else max(0.0, trade_source_age - trade_receive_age)
        )
        book_lag = (
            None
            if book_source_age is None or book_receive_age is None
            else max(0.0, book_source_age - book_receive_age)
        )
        return {
            "cache_has_state": True,
            "last_trade_at": (
                state.last_trade_at.isoformat() if state.last_trade_at else None
            ),
            "last_book_at": (
                state.last_book_at.isoformat() if state.last_book_at else None
            ),
            "last_trade_source_at": (
                state.last_trade_source_at.isoformat()
                if state.last_trade_source_at
                else None
            ),
            "last_book_source_at": (
                state.last_book_source_at.isoformat()
                if state.last_book_source_at
                else None
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
            "trade_receive_age_sec": (
                round(trade_receive_age, 3) if trade_receive_age is not None else None
            ),
            "book_receive_age_sec": (
                round(book_receive_age, 3) if book_receive_age is not None else None
            ),
            "trade_source_age_sec": (
                round(trade_source_age, 3) if trade_source_age is not None else None
            ),
            "book_source_age_sec": (
                round(book_source_age, 3) if book_source_age is not None else None
            ),
            "trade_consumer_lag_sec": (
                round(trade_lag, 3) if trade_lag is not None else None
            ),
            "book_consumer_lag_sec": (
                round(book_lag, 3) if book_lag is not None else None
            ),
        }

    def is_trade_fresh(self, symbol: str, max_age_seconds: float) -> bool:
        state = self._state.get(symbol)
        if state is None or state.last_trade_received_at is None:
            return False
        return (
            datetime.now(timezone.utc) - state.last_trade_received_at
        ).total_seconds() <= max_age_seconds

    def is_book_fresh(self, symbol: str, max_age_seconds: float) -> bool:
        state = self._state.get(symbol)
        if state is None or state.last_book_received_at is None:
            return False
        return (
            datetime.now(timezone.utc) - state.last_book_received_at
        ).total_seconds() <= max_age_seconds

    def _maybe_log_freshness(self, symbol: str) -> None:
        now = datetime.now(timezone.utc)
        previous = self._last_freshness_log_at.get(symbol)
        if previous is not None and (now - previous).total_seconds() < 30.0:
            return
        self._last_freshness_log_at[symbol] = now
        logger.info(
            "live scanner freshness",
            extra={
                "aitos_extra": {"symbol": symbol, **self.freshness_snapshot(symbol)}
            },
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
