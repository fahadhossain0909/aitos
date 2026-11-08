"""Leakage-resistant walk-forward evaluation for candidate strategies."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aitos.backtest.engine import BacktestEngine, BacktestResult

from .metric_integrity import metric_integrity_error, validate_count


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
        validate_count(min_positive_windows, "min_positive_windows")
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
        for name, value in (
            ("train_days", train_days),
            ("test_days", test_days),
            ("holdout_days", holdout_days),
        ):
            validate_count(value, name, minimum=1)
        for value in (start, end):
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError("walk-forward boundaries must be timezone-aware")
        if end <= start:
            raise ValueError("walk-forward end must follow start")
        ordered = list(events)
        if any(
            not isinstance(event.timestamp, datetime)
            or event.timestamp.utcoffset() is None
            for event in ordered
        ):
            raise ValueError("walk-forward event timestamps must be timezone-aware")
        ordered.sort(key=lambda e: e.timestamp)
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
                ).run(test_events, copy.deepcopy(strategy), mark_price)
                windows.append(
                    WindowResult(
                        train_start, train_end, cursor, current_test_end, result
                    )
                )
            cursor = current_test_end
        for window in windows:
            invalid = metric_integrity_error(window.result.metrics)
            if invalid:
                return WalkForwardResult(
                    tuple(windows),
                    None,
                    False,
                    f"invalid walk-forward metrics: {invalid}",
                )
        holdout_start = end - timedelta(days=holdout_days)
        holdout_events = self._window(ordered, holdout_start, end)
        holdout = (
            BacktestEngine(self.initial_cash, self.fee_rate, self.slippage_bps).run(
                holdout_events, copy.deepcopy(strategy), mark_price
            )
            if holdout_events
            else None
        )
        if holdout is not None:
            invalid = metric_integrity_error(holdout.metrics)
            if invalid:
                return WalkForwardResult(
                    tuple(windows),
                    holdout,
                    False,
                    f"invalid locked holdout metrics: {invalid}",
                )
        positive = sum(1 for w in windows if w.result.metrics.total_return > 0)
        # A locked holdout is mandatory: treating a missing holdout as a
        # pass silently downgrades out-of-sample validation to in-sample/
        # walk-forward-only evidence.
        passed = (
            bool(windows)
            and holdout is not None
            and positive >= self.min_positive_windows
            and holdout.metrics.total_return > 0
        )
        if not windows:
            reason = "no walk-forward test windows available"
        elif holdout is None:
            reason = "locked holdout has no events"
        elif positive < self.min_positive_windows:
            reason = "insufficient positive walk-forward windows"
        elif holdout.metrics.total_return <= 0:
            reason = "locked holdout return is not positive"
        else:
            reason = "walk-forward and locked holdout passed"
        return WalkForwardResult(tuple(windows), holdout, passed, reason)
