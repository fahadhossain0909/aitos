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

LIVE_LIQUIDITY_GROUP = "market-scanner"
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
    """Maintain scanner state from one canonical market-data subscription path."""

    def __init__(
        self, event_bus: EventBus, symbols: list[str], max_trades: int = 5000
    ) -> None:
        self._bus = event_bus
        self._symbols = set(symbols)
        self._max_trades = max(100, max_trades)
        self._state: dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._initialized = False

    def _cache(self, symbol: str) -> LiveSymbolCache:
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    async def initialize(self, direct_market_data: bool = False) -> None:
        """Subscribe only to canonical V1 channels; direct mode is ignored."""
        if self._initialized:
            return
        for symbol in self._symbols:
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.trade.{symbol}",
                    self._on_trade,
                    group="market-scanner",
                    start_id="$",
                )
            )
            self._subscriptions.append(
                await self._bus.subscribe(
                    f"market.orderbook.{symbol}",
                    self._on_book,
                    group="market-scanner",
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
        """Deprecated compatibility hook; live state comes only from V1 Redis."""
        logger.debug(
            "ignored legacy live trade callback",
            extra={"aitos_extra": {"symbol": trade.symbol}},
        )

    async def accept_live_order_book(self, book: OrderBookSnapshot) -> None:
        """Deprecated compatibility hook; live state comes only from V1 Redis."""
        logger.debug(
            "ignored legacy live orderbook callback",
            extra={"aitos_extra": {"symbol": book.symbol}},
        )

    async def _on_trade(self, event: Event) -> None:
        trade = TradeTick.from_dict(event.payload)
        received_at = datetime.now(timezone.utc)
        source_age = (received_at - trade.timestamp).total_seconds()
        state = self._cache(trade.symbol)
        if source_age > LIVE_TRADE_MAX_AGE_SECONDS:
            state.stale_trade_rejections += 1
            state.last_stale_trade_source_age_sec = round(max(0.0, source_age), 3)
            logger.warning(
                "discarded stale canonical trade",
                extra={
                    "aitos_extra": {
                        "symbol": trade.symbol,
                        "source_age_sec": round(max(0.0, source_age), 3),
                    }
                },
            )
            return
        if state.trades and trade.trade_id <= state.trades[-1].trade_id:
            state.duplicate_trade_rejections += 1
            return
        state.trades.append(trade)
        state.last_trade_at = received_at
        state.last_trade_source_at = trade.timestamp
        state.last_trade_received_at = received_at

    async def _on_book(self, event: Event) -> None:
        book = OrderBookSnapshot.from_dict(event.payload)
        received_at = datetime.now(timezone.utc)
        state = self._cache(book.symbol)
        if (
            state.order_book is not None
            and state.order_book.last_update_id == book.last_update_id
            and state.order_book.timestamp == book.timestamp
        ):
            state.duplicate_book_rejections += 1
            return
        state.order_book = book
        state.last_book_at = received_at
        state.last_book_source_at = book.timestamp
        state.last_book_received_at = received_at

    async def _on_liquidity(self, event: Event) -> None:
        payload = dict(event.payload)
        symbol = payload.get("symbol") or event.topic.rsplit(".", 1)[-1]
        self._cache(symbol).liquidity_events.append(payload)

    def snapshot(self, symbol: str) -> LiveSymbolCache | None:
        return self._state.get(symbol)
