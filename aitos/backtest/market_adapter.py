"""Historical market-event adapter for the shared AITOS intelligence pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aitos.intelligence.footprint import FootprintEngine
from aitos.intelligence.footprint_signals import (FootprintSignalEngine,
                                                  FootprintSignals)
from aitos.intelligence.liquidity_tracker import (LiquidityEvent,
                                                  LiquidityTracker)
from aitos.intelligence.live_auction import live_auction_score
from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.intelligence.orderflow_liquidity_interaction import (
    FlowLiquidityInteractionEngine, FlowLiquiditySignal)
from aitos.models.market import OrderBookSnapshot, TradeTick


@dataclass(frozen=True)
class HistoricalMarketState:
    symbol: str
    latest_trade: TradeTick | None
    latest_order_book: OrderBookSnapshot | None
    footprint_signals: FootprintSignals | None
    liquidity_events: tuple[LiquidityEvent, ...]
    flow_liquidity_signal: FlowLiquiditySignal | None
    auction_long_score: float
    auction_short_score: float


class HistoricalMarketAdapter:
    """Feed historical trades/L2 through the same live intelligence primitives."""

    def __init__(self, symbol: str, tick_size: float, trade_window: int = 500) -> None:
        if trade_window <= 0:
            raise ValueError("trade_window must be positive")
        self.symbol = symbol
        self.order_flow = OrderFlowEngine(max_trades=trade_window)
        self.footprint = FootprintEngine(tick_size=tick_size)
        self.footprint_signals = FootprintSignalEngine()
        self.liquidity = LiquidityTracker()
        self.interaction = FlowLiquidityInteractionEngine()
        self._trades: list[TradeTick] = []
        self._latest_trade: TradeTick | None = None
        self._latest_book: OrderBookSnapshot | None = None
        self._last_signals: FootprintSignals | None = None
        self._liquidity_events: list[LiquidityEvent] = []
        self._interaction_signal: FlowLiquiditySignal | None = None

    def on_trade(self, trade: TradeTick) -> None:
        if trade.symbol != self.symbol:
            raise ValueError("trade symbol does not match adapter symbol")
        self.order_flow.ingest(trade)
        self._trades.append(trade)
        if len(self._trades) > self.order_flow.max_trades:
            self._trades.pop(0)
        self._latest_trade = trade
        self._last_signals = self.footprint_signals.evaluate(
            self.footprint.build(self._trades)
        )
        if self._last_signals and self._liquidity_events:
            self._interaction_signal = self.interaction.evaluate(
                self._last_signals, self._liquidity_events
            )

    def on_order_book(self, book: OrderBookSnapshot) -> None:
        if book.symbol != self.symbol:
            raise ValueError("order-book symbol does not match adapter symbol")
        self._latest_book = book
        events = self.liquidity.update(book, self._trades)
        self._liquidity_events.extend(events)
        self._liquidity_events = self._liquidity_events[-200:]
        if self._last_signals:
            self._interaction_signal = self.interaction.evaluate(
                self._last_signals, self._liquidity_events
            )

    def state(self) -> HistoricalMarketState:
        return HistoricalMarketState(
            symbol=self.symbol,
            latest_trade=self._latest_trade,
            latest_order_book=self._latest_book,
            footprint_signals=self._last_signals,
            liquidity_events=tuple(self._liquidity_events),
            flow_liquidity_signal=self._interaction_signal,
            auction_long_score=live_auction_score(
                self._trades, self._latest_book, "long"
            ),
            auction_short_score=live_auction_score(
                self._trades, self._latest_book, "short"
            ),
        )

    def feed(
        self, events: Iterable[TradeTick | OrderBookSnapshot]
    ) -> HistoricalMarketState:
        for event in sorted(events, key=lambda item: item.timestamp):
            if isinstance(event, TradeTick):
                self.on_trade(event)
            elif isinstance(event, OrderBookSnapshot):
                self.on_order_book(event)
            else:
                raise TypeError(f"unsupported market event: {type(event).__name__}")
        return self.state()
