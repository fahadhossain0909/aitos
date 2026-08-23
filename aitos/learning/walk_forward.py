"""Leakage-resistant walk-forward evaluation for candidate strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence

from aitos.backtest.engine import BacktestEngine, BacktestResult


@dataclass(frozen=True)
class WindowResult:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    result: BacktestResult


@dataclass(frozen=True)
class WalkForwardResult:
    windows: tuple[WindowResult, ...]
    locked_holdout: BacktestResult | None
    passed: bool
    reason: str


class WalkForwardValidator:
    """Evaluate a candidate on sequential unseen windows.

    The validator never tunes a candidate. It only evaluates a strategy that
    has already been proposed, which keeps the holdout genuinely out of the
    evolution loop.
    """

    def __init__(
        self,
        initial_cash: float = 10_000.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 0.0,
        min_positive_windows: int = 1,
    ) -> None:
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.min_positive_windows = min_positive_windows

    @staticmethod
    def _window(events: Sequence[Any], start: datetime, end: datetime) -> list[Any]:
        return [e for e in events if start <= e.timestamp < end]

    def evaluate(
        self,
        events: Iterable[Any],
        strategy: Callable,
        mark_price: Callable,
        start: datetime,
        end: datetime,
        train_days: int = 180,
        test_days: int = 30,
        holdout_days: int = 30,
    ) -> WalkForwardResult:
        ordered = sorted(events, key=lambda e: e.timestamp)
        windows: list[WindowResult] = []
        cursor = start + timedelta(days=train_days)
        test_end = end - timedelta(days=holdout_days)
        while cursor + timedelta(days=test_days) <= test_end:
            train_start = cursor - timedelta(days=train_days)
            train_end = cursor
            current_test_end = cursor + timedelta(days=test_days)
            # Training data is intentionally not passed to the deterministic
            # evaluator here; it is metadata defining what was available.
            test_events = self._window(ordered, cursor, current_test_end)
            if test_events:
                result = BacktestEngine(
                    self.initial_cash, self.fee_rate, self.slippage_bps
                ).run(test_events, strategy, mark_price)
                windows.append(
                    WindowResult(
                        train_start, train_end, cursor, current_test_end, result
                    )
                )
            cursor = current_test_end
        holdout_start = end - timedelta(days=holdout_days)
        holdout_events = self._window(ordered, holdout_start, end)
        holdout = (
            BacktestEngine(self.initial_cash, self.fee_rate, self.slippage_bps).run(
                holdout_events, strategy, mark_price
            )
            if holdout_events
            else None
        )
        positive = sum(1 for w in windows if w.result.metrics.total_return > 0)
        passed = (
            bool(windows)
            and positive >= self.min_positive_windows
            and (holdout is None or holdout.metrics.total_return > 0)
        )
        reason = (
            "walk-forward and locked holdout passed"
            if passed
            else "candidate failed walk-forward/holdout requirements"
        )
        return WalkForwardResult(tuple(windows), holdout, passed, reason)
