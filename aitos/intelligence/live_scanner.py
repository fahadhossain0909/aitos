"""Event-driven live market cache backed by canonical MarketData V1."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot, TradeTick
from aitos.market_data.bus import MarketDataBus, market_event_from_wire
from aitos.market_data.contracts import MarketEventType, MarketSource

logger = get_logger("aitos.intelligence.live_scanner")

LIVE_SCANNER_GROUP = "market-scanner"
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
    stale_trade_rejections: int = 0
    duplicate_trade_rejections: int = 0
    duplicate_book_rejections: int = 0
    last_stale_trade_source_age_sec: float | None = None


class LiveScannerCache:
    """Maintain scanner state from canonical semantic market-data channels."""

    def __init__(self, event_bus: EventBus, symbols: list[str], max_trades: int = 5000) -> None:
        self._bus = MarketDataBus(event_bus)
        self._symbols = {s.upper() for s in symbols}
        self._max_trades = max(100, max_trades)
        self._state: dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._initialized = False

    def _cache(self, symbol: str) -> LiveSymbolCache:
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    async def initialize(self, direct_market_data: bool = False) -> None:
        """Subscribe once per canonical event type; filter symbols locally."""
        if self._initialized:
            return
        self._subscriptions = [
            await self._bus.subscribe(
                MarketEventType.TRADE,
                self._on_trade_event,
                group=LIVE_SCANNER_GROUP,
                live_only=True,
            ),
            await self._bus.subscribe(
                MarketEventType.BOOK_SNAPSHOT,
                self._on_book_event,
                group=LIVE_SCANNER_GROUP,
                live_only=True,
            ),
        ]
        self._initialized = True
        logger.info(
            "canonical scanner subscriptions initialized",
            extra={"aitos_extra": {"symbols": sorted(self._symbols), "group": LIVE_SCANNER_GROUP}},
        )

    async def shutdown(self) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def accept_live_trade(self, trade: TradeTick) -> None:
        logger.debug("ignored legacy live trade callback", extra={"aitos_extra": {"symbol": trade.symbol}})

    async def accept_live_order_book(self, book: OrderBookSnapshot) -> None:
        logger.debug("ignored legacy live orderbook callback", extra={"aitos_extra": {"symbol": book.symbol}})

    async def _on_trade_event(self, event) -> None:
        if event.symbol not in self._symbols:
            return
        state = self._cache(event.symbol)
        received_at = datetime.now(timezone.utc)
        source_age = (received_at - event.event_time).total_seconds()
        if event.source != MarketSource.WEBSOCKET or source_age > LIVE_TRADE_MAX_AGE_SECONDS:
            state.stale_trade_rejections += 1
            state.last_stale_trade_source_age_sec = round(max(0.0, source_age), 3)
            return
        trade = TradeTick.from_dict(dict(event.payload))
        if state.trades and trade.trade_id <= state.trades[-1].trade_id:
            state.duplicate_trade_rejections += 1
            return
        state.trades.append(trade)
        state.last_trade_at = received_at
        state.last_trade_source_at = event.event_time
        state.last_trade_received_at = received_at

    async def _on_book_event(self, event) -> None:
        if event.symbol not in self._symbols:
            return
        state = self._cache(event.symbol)
        received_at = datetime.now(timezone.utc)
        payload = dict(event.payload)
        payload["symbol"] = event.symbol
        payload["timestamp"] = event.event_time.isoformat()
        book = OrderBookSnapshot.from_dict(payload)
        if state.order_book is not None and state.order_book.last_update_id == book.last_update_id and state.order_book.timestamp == book.timestamp:
            state.duplicate_book_rejections += 1
            return
        state.order_book = book
        state.last_book_at = received_at
        state.last_book_source_at = event.event_time
        state.last_book_received_at = received_at

    async def _on_liquidity(self, event) -> None:
        if event.symbol in self._symbols:
            self._cache(event.symbol).liquidity_events.append(dict(event.payload))

    def snapshot(self, symbol: str) -> LiveSymbolCache | None:
        return self._state.get(symbol)
