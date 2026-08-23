"""Shared live state for order flow and L2 liquidity intelligence."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

from aitos.intelligence.liquidity_tracker import (LiquidityEvent,
                                                  LiquidityTracker)
from aitos.intelligence.order_flow_engine import (OrderFlowEngine,
                                                  OrderFlowFeatures)
from aitos.models.market import OrderBookSnapshot, TradeTick


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
        self._trades[trade.symbol].append(trade)
        return self._flow[trade.symbol].ingest(trade)

    def on_order_book(self, book: OrderBookSnapshot) -> tuple[LiquidityEvent, ...]:
        events = tuple(
            self._liquidity[book.symbol].update(book, tuple(self._trades[book.symbol]))
        )
        self._events[book.symbol].extend(events)
        self._books[book.symbol] = book
        return events

    def snapshot(self, symbol: str) -> LiveMarketState:
        return LiveMarketState(
            self._flow[symbol].snapshot(),
            tuple(self._events[symbol]),
            self._books.get(symbol),
            len(self._trades[symbol]),
        )
