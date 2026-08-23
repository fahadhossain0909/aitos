"""End-to-end historical runner with L2 execution and futures margin."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable
from aitos.backtest.market_adapter import HistoricalMarketAdapter, HistoricalMarketState
from aitos.backtest.replay import MarketReplay
from aitos.backtest.l2_execution import BookLevel, L2ExecutionModel
from aitos.backtest.execution import ExecutionSimulator
from aitos.backtest.queue_lifecycle import QueueOrderLifecycle, SimulatedOrder
from aitos.backtest.margin import PerpetualMarginModel
from aitos.models.market import TradeTick, OrderBookSnapshot

@dataclass(frozen=True)
class HistoricalDecision:
    direction: str
    confidence: float
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None
    queue_ahead: float = 0.0

@dataclass(frozen=True)
class HistoricalRunResult:
    states: int
    decisions: int
    fills: int
    requested_quantity: float
    filled_quantity: float
    final_equity: float
    total_return: float
    total_fees: float
    funding_paid: float
    liquidated: bool
    passive_orders: int = 0
    passive_fills: int = 0

class AITOSHistoricalRunner:
    """Run shared historical intelligence with L2 execution and futures margin."""
    def __init__(self, symbol: str, tick_size: float, initial_cash: float,
                 fee_rate: float = 0.0004, slippage_bps: float = 0.0,
                 trade_window: int = 500, max_book_levels: int | None = None,
                 leverage: float = 1.0, maintenance_rate: float = 0.005) -> None:
        self.adapter = HistoricalMarketAdapter(symbol, tick_size, trade_window)
        self.execution = ExecutionSimulator(initial_cash, fee_rate, slippage_bps)
        self.l2 = L2ExecutionModel(max_levels=max_book_levels)
        self.queue = QueueOrderLifecycle()
        self.margin = PerpetualMarginModel(initial_cash, leverage, maintenance_rate)
        self.initial_cash = initial_cash
        self._order_seq = 0

    def _apply_fill(self, side: str, quantity: float, price: float) -> None:
        if quantity <= 0:
            return
        self.execution.execute(side, quantity, price)
        self.margin.open_or_add(quantity if side == "buy" else -quantity, price)

    def run(self, events: Iterable[TradeTick | OrderBookSnapshot],
            decide: Callable[[HistoricalMarketState], HistoricalDecision],
            funding_rate: Callable[[datetime], float] | None = None) -> HistoricalRunResult:
        ordered = MarketReplay(events)
        states = decisions = fills = passive_orders = passive_fills = 0
        requested = filled = 0.0
        last_price = 0.0
        last_funding_time = None
        for event in ordered.events:
            if isinstance(event, TradeTick):
                self.adapter.on_trade(event)
                last_price = event.price
                trade_side = "sell" if event.is_buyer_maker else "buy"
                for pf in self.queue.consume(trade_side, event.price, event.quantity, event.timestamp):
                    order = self.queue.orders[pf.order_id]
                    self._apply_fill(order.side, pf.quantity, pf.price)
                    filled += pf.quantity; fills += 1; passive_fills += 1
            else:
                self.adapter.on_order_book(event)
                if event.best_bid > 0 and event.best_ask > 0:
                    last_price = (event.best_bid + event.best_ask) / 2.0
            if funding_rate is not None and last_funding_time != event.timestamp:
                if last_price > 0:
                    self.margin.apply_funding(funding_rate(event.timestamp), last_price)
                last_funding_time = event.timestamp
            if last_price > 0 and self.margin.check_liquidation(last_price):
                break
            state = self.adapter.state(); states += 1
            decision = decide(state); decisions += 1
            if decision.quantity <= 0 or decision.direction not in {"long", "short"}:
                continue
            side = "buy" if decision.direction == "long" else "sell"
            requested += decision.quantity
            if decision.order_type == "limit":
                if decision.limit_price is None or decision.limit_price <= 0:
                    continue
                self._order_seq += 1
                self.queue.place(SimulatedOrder(f"bt-{self._order_seq}", side, decision.limit_price,
                                                decision.quantity, decision.quantity,
                                                max(0.0, decision.queue_ahead), event.timestamp))
                passive_orders += 1
                continue
            book = state.latest_order_book
            if book is None:
                continue
            bids = [BookLevel(level.price, level.quantity) for level in book.bids]
            asks = [BookLevel(level.price, level.quantity) for level in book.asks]
            result = self.l2.execute(side, decision.quantity, bids, asks)
            if result.filled_quantity > 0:
                self._apply_fill(side, result.filled_quantity, result.average_price)
                filled += result.filled_quantity; fills += 1; last_price = result.average_price
        snap = self.margin.snapshot(last_price) if last_price > 0 else None
        final_equity = snap.equity if snap else self.initial_cash
        return HistoricalRunResult(states, decisions, fills, requested, filled, final_equity,
                                   final_equity / self.initial_cash - 1.0, self.execution.fees,
                                   self.margin.funding_paid, self.margin.liquidated,
                                   passive_orders, passive_fills)
