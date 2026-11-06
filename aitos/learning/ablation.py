"""Canonical historical replay ablation runner with LONG/SHORT reporting."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aitos.backtest.engine import BacktestEngine, BacktestResult
from aitos.intelligence.adaptive_mtf import walk_forward_metrics
from aitos.learning.walk_forward import WalkForwardResult, WalkForwardValidator


@dataclass(frozen=True)
class AblationVariantResult:
    name: str
    replay: BacktestResult
    walk_forward: WalkForwardResult
    directional_metrics: dict[str, dict[str, float | None]]
    unclassified_trade_count: int


@dataclass(frozen=True)
class AblationReport:
    baseline: str
    variants: Mapping[str, AblationVariantResult]
    deltas_vs_baseline: Mapping[str, Mapping[str, float | None]]


def _directional_metrics(result: BacktestResult) -> tuple[dict[str, dict[str, float | None]], int]:
    groups: dict[str, list[dict[str, float]]] = {"LONG": [], "SHORT": []}
    unclassified = 0
    for trade in result.trades:
        side = str(trade.fields.get("side", "")).upper()
        if side not in groups:
            unclassified += 1
            continue
        groups[side].append({"net_pnl": float(trade.reward)})
    return (
        {side: walk_forward_metrics(rows) for side, rows in groups.items()},
        unclassified,
    )


class HistoricalAblationRunner:
    """Run baseline and component-disabled strategies through the canonical engine.

    Each variant receives the same immutable event sequence and execution
    assumptions. Walk-forward windows and the locked holdout are evaluated
    independently per variant. No candidate is promoted automatically.
    """

    def __init__(
        self,
        *,
        initial_cash: float = 10_000.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 0.0,
        train_days: int = 180,
        test_days: int = 30,
        holdout_days: int = 30,
        min_positive_windows: int = 1,
    ) -> None:
        if min(train_days, test_days, holdout_days) <= 0:
            raise ValueError("walk-forward window lengths must be positive")
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.train_days = train_days
        self.test_days = test_days
        self.holdout_days = holdout_days
        self.min_positive_windows = min_positive_windows

    def run(
        self,
        events: Iterable[Any],
        strategies: Mapping[str, Callable[[Any, Any], None]],
        mark_price: Callable[[Any], float],
        *,
        baseline: str = "baseline",
    ) -> AblationReport:
        ordered = tuple(sorted(events, key=lambda event: event.timestamp))
        if not ordered:
            raise ValueError("historical replay requires at least one event")
        if baseline not in strategies:
            raise ValueError(f"baseline strategy '{baseline}' is missing")
        if len(strategies) < 2:
            raise ValueError("ablation requires a baseline and at least one variant")
        if any(not name or not callable(strategy) for name, strategy in strategies.items()):
            raise ValueError("each ablation variant must have a name and callable strategy")

        start = ordered[0].timestamp
        last = ordered[-1].timestamp
        end = last.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        results: dict[str, AblationVariantResult] = {}
        for name, strategy in strategies.items():
            replay = BacktestEngine(
                self.initial_cash, self.fee_rate, self.slippage_bps
            ).run(ordered, strategy, mark_price)
            walk_forward = WalkForwardValidator(
                initial_cash=self.initial_cash,
                fee_rate=self.fee_rate,
                slippage_bps=self.slippage_bps,
                min_positive_windows=self.min_positive_windows,
            ).evaluate(
                ordered,
                strategy,
                mark_price,
                start=start,
                end=end,
                train_days=self.train_days,
                test_days=self.test_days,
                holdout_days=self.holdout_days,
            )
            directional, unclassified = _directional_metrics(replay)
            results[name] = AblationVariantResult(
                name, replay, walk_forward, directional, unclassified
            )

        baseline_result = results[baseline]
        base_metrics = baseline_result.replay.metrics
        deltas: dict[str, dict[str, float | None]] = {}
        for name, result in results.items():
            if name == baseline:
                continue
            metrics = result.replay.metrics
            delta: dict[str, float | None] = {
                "total_return": metrics.total_return - base_metrics.total_return,
                "max_drawdown": metrics.max_drawdown - base_metrics.max_drawdown,
                "sharpe": metrics.sharpe - base_metrics.sharpe,
            }
            for side in ("LONG", "SHORT"):
                base_exp = baseline_result.directional_metrics[side]["expectancy"]
                variant_exp = result.directional_metrics[side]["expectancy"]
                delta[f"{side.lower()}_expectancy"] = (
                    variant_exp - base_exp
                    if base_exp is not None and variant_exp is not None
                    else None
                )
            deltas[name] = delta
        return AblationReport(baseline, results, deltas)
