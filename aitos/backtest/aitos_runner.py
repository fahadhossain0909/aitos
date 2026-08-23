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

class AITOSH​istoricalRunner:
    """Run shared historical intelligence with L2 execution and futures margin."""
