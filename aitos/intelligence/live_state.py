"""Shared live state for order flow and L2 liquidity intelligence."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from aitos.intelligence.liquidity_intelligence import LiquidityLevel

from aitos.intelligence.liquidity_tracker import LiquidityEvent, LiquidityTracker
from aitos.intelligence.order_flow_engine import OrderFlowEngine, OrderFlowFeatures
from aitos.models.market import OrderBookSnapshot, TradeTick

LIVE_TRADE_MAX_AGE_SECONDS = 15.0


@dataclass(frozen=True)
class LiveMarketState:
    order_flow: OrderFlowFeatures | None
    liquidity_events: tuple[LiquidityEvent, ...]
    order_book: OrderBookSnapshot | None
    trade_count: int
    liquidity_levels: tuple[LiquidityLevel, ...] = ()


class LiveMarketStateStore:
    def __init__(
        self,
        max_trades: int = 5000,
        max_liquidity_events: int = 100,
        max_liquidity_levels: int = 80,
        liquidity_level_max_age_seconds: float = 1800.0,
    ) -> None:
        if max_liquidity_levels < 1 or liquidity_level_max_age_seconds <= 0:
            raise ValueError("liquidity level limits must be positive")
        self._max_liquidity_levels = max_liquidity_levels
        self._liquidity_level_max_age = timedelta(
            seconds=liquidity_level_max_age_seconds
        )
        self._trades: dict[str, deque[TradeTick]] = defaultdict(
            lambda: deque(maxlen=max_trades)
        )
        self._flow: dict[str, OrderFlowEngine] = defaultdict(
            lambda: OrderFlowEngine(max_trades=max_trades)
        )
        self._liquidity: dict[str, LiquidityTracker] = defaultdict(LiquidityTracker)
        self._events: dict[str, deque[LiquidityEvent]] = defaultdict(
            lambda: deque(maxlen=max_liquidity_events)
        )
        self._books: dict[str, OrderBookSnapshot] = {}
        self._levels: dict[str, tuple[LiquidityLevel, ...]] = {}
        self._levels_updated_at: dict[str, datetime] = {}

    @property
    def trades(self) -> dict[str, deque[TradeTick]]:
        """Recent trade buffers, exposed read/write for legacy ingestion callers."""
        return self._trades

    def on_trade(self, trade: TradeTick) -> OrderFlowFeatures:
        # This store feeds live order-flow/liquidity state, so a delayed REST
        # recovery sample must never move the live cursor backwards. Historical
        # data can still be persisted elsewhere and replayed explicitly.
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=LIVE_TRADE_MAX_AGE_SECONDS
        )
        if trade.timestamp < cutoff:
            return self._flow[trade.symbol].snapshot()
        self._trades[trade.symbol].append(trade)
        return self._flow[trade.symbol].ingest(trade)

    def on_order_book(self, book: OrderBookSnapshot) -> tuple[LiquidityEvent, ...]:
        events = tuple(
            self._liquidity[book.symbol].update(book, tuple(self._trades[book.symbol]))
        )
        self._events[book.symbol].extend(events)
        self._books[book.symbol] = book
        return events

    def update_liquidity_levels(
        self,
        symbol: str,
        levels: tuple[LiquidityLevel, ...] | list[LiquidityLevel],
        *,
        updated_at: datetime | None = None,
    ) -> tuple[LiquidityLevel, ...]:
        """Atomically replace one symbol's structural liquidity snapshot.

        Only levels for the requested symbol are retained. The snapshot is
        bounded and expires so stale historical pools cannot drive live stops.
        """
        normalized = str(symbol).upper()
        unique: dict[str, LiquidityLevel] = {}
        for level in levels:
            if level.symbol.upper() != normalized:
                continue
            unique[level.level_id] = level
        selected = tuple(
            sorted(
                unique.values(),
                key=lambda item: (-item.strength, item.price),
            )[: self._max_liquidity_levels]
        )
        timestamp = updated_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        self._levels[normalized] = selected
        self._levels_updated_at[normalized] = timestamp.astimezone(timezone.utc)
        return selected

    def snapshot(self, symbol: str) -> dict[str, object]:
        """Return a dict-shaped snapshot suitable for Event.payload."""
        return asdict(self.snapshot_model(symbol))

    def snapshot_model(self, symbol: str) -> LiveMarketState:
        """Return the typed in-memory live-state model."""
        normalized = str(symbol).upper()
        updated_at = self._levels_updated_at.get(normalized)
        levels = self._levels.get(normalized, ())
        if (
            updated_at is None
            or datetime.now(timezone.utc) - updated_at > self._liquidity_level_max_age
        ):
            levels = ()
        return LiveMarketState(
            order_flow=self._flow[normalized].snapshot(),
            liquidity_events=tuple(self._events[normalized]),
            order_book=self._books.get(normalized),
            trade_count=len(self._trades[normalized]),
            liquidity_levels=levels,
        )
