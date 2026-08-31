"""Historical baseline-vs-hedged benchmark utilities.

This module intentionally does not invent market data. It consumes the same
historical event stream used by AITOSHistoricalRunner and runs two supplied
decision policies against independent execution models, making the comparison
apples-to-apples.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from aitos.backtest.aitos_runner import AITOSHistoricalRunner, HistoricalDecision
from aitos.backtest.hedge_metrics import HedgeComparison, TradeExcursion, compare
from aitos.backtest.market_adapter import HistoricalMarketState
from aitos.models.market import OrderBookSnapshot, TradeTick


@dataclass(frozen=True)
class BenchmarkConfig:
    symbol: str
    tick_size: float
    initial_cash: float
    fee_rate: float = 0.0004
    slippage_bps: float = 0.0
    leverage: float = 1.0
    maintenance_rate: float = 0.005


@dataclass(frozen=True)
class BenchmarkRun:
    result: object
    equity_curve: tuple[float, ...]
    trade_pnls: tuple[float, ...]
    excursions: tuple[TradeExcursion, ...]
    hedge_pnl: float = 0.0
    hedge_cost: float = 0.0
    hedge_count: int = 0


class HistoricalHedgeBenchmark:
    """Run identical historical data through baseline and hedged policies.

    The caller supplies policies.  ``baseline_policy`` must be the production
    decision policy with hedging disabled. ``hedged_policy`` must be the same
    policy with Conditional Hedge Intelligence enabled.  The benchmark never
    mixes their execution state.
    """

    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config

    def run(
        self,
        events: Iterable[TradeTick | OrderBookSnapshot],
        baseline_policy: Callable[[HistoricalMarketState], HistoricalDecision],
        hedged_policy: Callable[[HistoricalMarketState], HistoricalDecision],
        funding_rate: Callable[[datetime], float] | None = None,
    ) -> tuple[BenchmarkRun, BenchmarkRun, HedgeComparison]:
        materialized = tuple(events)
        baseline = AITOSHistoricalRunner(**self._runner_kwargs())
        hedged = AITOSHistoricalRunner(**self._runner_kwargs())

        baseline_result = baseline.run(materialized, baseline_policy, funding_rate)
        hedged_result = hedged.run(materialized, hedged_policy, funding_rate)

        # The rich runner currently exposes final equity/fees/funding but not a
        # per-event equity curve.  Preserve a one-point curve here until the
        # runner exposes snapshots; this prevents fabricated drawdown numbers.
        baseline_run = BenchmarkRun(
            baseline_result,
            (self.config.initial_cash, baseline_result.final_equity),
            (),
            (),
            hedge_pnl=0.0,
            hedge_cost=0.0,
            hedge_count=0,
        )
        hedged_run = BenchmarkRun(
            hedged_result,
            (self.config.initial_cash, hedged_result.final_equity),
            (),
            (),
            hedge_pnl=0.0,
            hedge_cost=0.0,
            hedge_count=0,
        )

        comparison = compare(
            baseline_run.equity_curve,
            hedged_run.equity_curve,
            baseline_run.trade_pnls,
            hedged_run.trade_pnls,
            baseline_run.excursions,
            hedged_run.excursions,
            hedge_pnl=hedged_run.hedge_pnl,
            hedge_cost=hedged_run.hedge_cost,
            hedge_count=hedged_run.hedge_count,
        )
        return baseline_run, hedged_run, comparison

    def _runner_kwargs(self) -> dict[str, object]:
        return {
            "symbol": self.config.symbol,
            "tick_size": self.config.tick_size,
            "initial_cash": self.config.initial_cash,
            "fee_rate": self.config.fee_rate,
            "slippage_bps": self.config.slippage_bps,
            "leverage": self.config.leverage,
            "maintenance_rate": self.config.maintenance_rate,
        }
