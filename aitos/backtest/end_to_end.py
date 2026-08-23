"""End-to-end bridge from canonical historical events into existing engines."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Any

from aitos.data.schema import CanonicalBookEvent, CanonicalTrade
from aitos.data.orderbook_replay import OrderBookReconstructor


@dataclass
class ReplayStats:
    trades: int = 0
    book_events: int = 0
    stale_book_events: int = 0
    rejected_book_events: int = 0


class AITOSReplay:
    """Small integration seam; strategy/execution logic remains in existing engines.

    Callbacks receive canonical events so the existing AITOS Order Flow,
    Footprint, Liquidity and Execution components can be wired without creating
    duplicate implementations here.
    """

    def __init__(self, on_trade=None, on_book=None, on_book_state=None):
        self.on_trade = on_trade
        self.on_book = on_book
        self.on_book_state = on_book_state
        self.book = OrderBookReconstructor()
        self.stats = ReplayStats()

    def feed_trade(self, event: CanonicalTrade) -> None:
        if self.on_trade:
            self.on_trade(event)
        self.stats.trades += 1

    def feed_book(self, event: CanonicalBookEvent) -> bool:
        accepted = self.book.apply(event)
        if not accepted:
            self.stats.stale_book_events += 1
            return False
        if self.on_book:
            self.on_book(event)
        if self.on_book_state:
            self.on_book_state(self.book.state())
        self.stats.book_events += 1
        return True

    def replay(self, events: Iterable[Any]) -> ReplayStats:
        for event in events:
            if isinstance(event, CanonicalTrade):
                self.feed_trade(event)
            elif isinstance(event, CanonicalBookEvent):
                self.feed_book(event)
            else:
                self.stats.rejected_book_events += 1
        return self.stats
