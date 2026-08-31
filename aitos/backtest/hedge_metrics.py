"""Metrics and deterministic comparison helpers for conditional hedging."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import inf


@dataclass(frozen=True)
class TradeExcursion:
    entry: float
    side: str
    prices: tuple[float, ...]
    mae: float
    mfe: float


def excursions(entry: float, side: str, prices: Sequence[float]) -> TradeExcursion:
    if entry <= 0 or not prices:
        return TradeExcursion(entry, side, tuple(prices), 0.0, 0.0)
    sign = 1.0 if side.upper() == "LONG" else -1.0
    returns = [sign * (p - entry) / entry for p in prices]
    return TradeExcursion(entry, side, tuple(prices), min(returns), max(returns))


def max_drawdown(equity: Sequence[float]) -> float:
    peak = -inf
    result = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            result = max(result, (peak - value) / peak)
    return result


def expectancy(pnls: Sequence[float]) -> float:
    return sum(pnls) / len(pnls) if pnls else 0.0


def aggregate_mae(excursions_: Sequence[TradeExcursion]) -> float:
    """Worst adverse excursion across trades."""
    return min((x.mae for x in excursions_), default=0.0)


def aggregate_mfe(excursions_: Sequence[TradeExcursion]) -> float:
    """Best favorable excursion across trades."""
    return max((x.mfe for x in excursions_), default=0.0)


@dataclass(frozen=True)
class HedgeComparison:
    baseline_net_pnl: float
    hedged_net_pnl: float
    baseline_max_drawdown: float
    hedged_max_drawdown: float
    baseline_mae: float
    hedged_mae: float
    baseline_mfe: float
    hedged_mfe: float
    hedge_cost: float
    hedge_pnl: float
    baseline_expectancy: float
    hedged_expectancy: float
    hedge_count: int

    @property
    def pnl_delta(self) -> float:
        return self.hedged_net_pnl - self.baseline_net_pnl

    @property
    def drawdown_reduction(self) -> float:
        if self.baseline_max_drawdown == 0:
            return 0.0
        return (
            self.baseline_max_drawdown - self.hedged_max_drawdown
        ) / self.baseline_max_drawdown

    @property
    def net_hedge_contribution(self) -> float:
        return self.hedge_pnl - self.hedge_cost

    @property
    def hedge_cost_ratio(self) -> float:
        if self.hedge_pnl == 0:
            return 0.0
        return self.hedge_cost / abs(self.hedge_pnl)


def compare(
    baseline_equity: Sequence[float],
    hedged_equity: Sequence[float],
    baseline_pnls: Sequence[float],
    hedged_pnls: Sequence[float],
    baseline_excursions: Sequence[TradeExcursion],
    hedged_excursions: Sequence[TradeExcursion],
    hedge_pnl: float,
    hedge_cost: float,
    hedge_count: int,
) -> HedgeComparison:
    """Compare paired baseline/hedged replay outputs without mixing samples."""
    return HedgeComparison(
        baseline_net_pnl=(
            (baseline_equity[-1] - baseline_equity[0]) if baseline_equity else 0.0
        ),
        hedged_net_pnl=(hedged_equity[-1] - hedged_equity[0]) if hedged_equity else 0.0,
        baseline_max_drawdown=max_drawdown(baseline_equity),
        hedged_max_drawdown=max_drawdown(hedged_equity),
        baseline_mae=aggregate_mae(baseline_excursions),
        hedged_mae=aggregate_mae(hedged_excursions),
        baseline_mfe=aggregate_mfe(baseline_excursions),
        hedged_mfe=aggregate_mfe(hedged_excursions),
        hedge_cost=hedge_cost,
        hedge_pnl=hedge_pnl,
        baseline_expectancy=expectancy(baseline_pnls),
        hedged_expectancy=expectancy(hedged_pnls),
        hedge_count=hedge_count,
    )
