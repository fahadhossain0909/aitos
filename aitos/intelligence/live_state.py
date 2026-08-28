"""Shared live state for order flow and L2 liquidity intelligence."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Deque, Dict, Optional

from aitos.intelligence.liquidity_tracker import LiquidityEvent, LiquidityTracker
from aitos.intelligence.order_flow_engine import OrderFlowEngine, OrderFlowFeatures
from aitos.models.market import OrderBookSnapshot, TradeTick


LIVE_TRADE_MAX_AGE_SECONDS = 15.0


@dataclass(frozen=True)
class LiveMarketState:
    order_flow: Optional[OrderFlowFeatures]
    liquidity_events: tuple[LiquidityEvent, ...]
    order_book: Optional[OrderBookSnapshot]
    trade_count: int


class LiveMarketStateStore:
    def __init__(self, max_trades: int = 5000, max_liquidity_events: int = 100) -> None:
        self._trades: Dict[str, Deque[TradeTick]] = defaultdict(
            lambda: deque(maxlen=max_trades)
        )
        self._flow: Dict[str, OrderFlowEngine] = defaultdict(
            lambda: OrderFlowEngine(max_trades=max_trades)
        )
        self._liquidity: Dict[str, LiquidityTracker] = defaultdict(LiquidityTracker)
        self._events: Dict[str, Deque[LiquidityEvent]] = defaultdict(
            lambda: deque(maxlen=max_liquidity_events)
        )
        self._books: Dict[str, OrderBookSnapshot] = {}

    @property
    def trades(self) -> Dict[str, Deque[TradeTick]]:
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

    def snapshot(self, symbol: str) -> Mapping[str, object]:
        """Return a dict-shaped snapshot suitable for Event.payload.

        Event payloads cross the module boundary as dictionaries. Keep the
        internal dataclass available via ``snapshot_model`` while making the
        public snapshot serialization-safe for the event bus.
        """
        return asdict(self.snapshot_model(symbol))

    def snapshot_model(self, symbol: str) -> LiveMarketState:
        """Return the typed in-memory live-state model."""
        return LiveMarketState(
            self._flow[symbol].snapshot(),
            tuple(self._events[symbol]),
            self._books.get(symbol),
            len(self._trades[symbol]),
        )
