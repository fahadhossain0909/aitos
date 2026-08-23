"""Validation gate around the canonical ProjectAlpha backtest engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from aitos.backtest.engine import BacktestEngine, BacktestResult

from .walk_forward import WalkForwardResult, WalkForwardValidator


@dataclass(frozen=True)
class ValidationPolicy:
    min_total_return: float = 0.0
    max_drawdown: float = 0.25
    min_sharpe: float = 0.0
    min_trades: int = 1
    require_improvement_over_champion: bool = True
    require_walk_forward: bool = True
    train_days: int = 180
    test_days: int = 30
    holdout_days: int = 30
    min_positive_windows: int = 1


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    reason: str
    candidate: BacktestResult
    champion: BacktestResult | None = None
    walk_forward: WalkForwardResult | None = None


class CandidateValidator:
    """Evaluate candidates with the canonical engine and leakage-resistant validation."""

    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()

    def evaluate(
        self,
        candidate_events: Iterable[Any],
        candidate_strategy: Callable,
        mark_price: Callable,
        champion_result: BacktestResult | None = None,
        initial_cash: float = 10_000.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 0.0,
    ) -> ValidationResult:
        events = sorted(candidate_events, key=lambda e: e.timestamp)
        candidate = BacktestEngine(initial_cash, fee_rate, slippage_bps).run(
            events, candidate_strategy, mark_price
        )
        m = candidate.metrics
        if m.total_return < self.policy.min_total_return:
            return ValidationResult(
                False, "candidate return below minimum", candidate, champion_result
            )
        if m.max_drawdown > self.policy.max_drawdown:
            return ValidationResult(
                False, "candidate drawdown above maximum", candidate, champion_result
            )
        if m.sharpe < self.policy.min_sharpe:
            return ValidationResult(
                False, "candidate Sharpe below minimum", candidate, champion_result
            )
        if m.trades < self.policy.min_trades:
            return ValidationResult(
                False, "insufficient candidate trades", candidate, champion_result
            )
        if (
            self.policy.require_improvement_over_champion
            and champion_result is not None
        ):
            cm = champion_result.metrics
            if m.total_return <= cm.total_return and m.max_drawdown >= cm.max_drawdown:
                return ValidationResult(
                    False,
                    "candidate does not improve return or drawdown",
                    candidate,
                    champion_result,
                )

        walk_forward: WalkForwardResult | None = None
        if self.policy.require_walk_forward:
            if not events:
                return ValidationResult(
                    False,
                    "no events available for walk-forward validation",
                    candidate,
                    champion_result,
                )
            start = min(e.timestamp for e in events)
            end = max(e.timestamp for e in events)
            # The end boundary is exclusive, so include the final event's day.
            end = end.replace(hour=0, minute=0, second=0, microsecond=0)
            end = end.replace(
                day=end.day
            )  # explicit normalization for static type checkers
            if end <= start:
                return ValidationResult(
                    False,
                    "insufficient temporal span for walk-forward validation",
                    candidate,
                    champion_result,
                )
            from datetime import timedelta

            end = end + timedelta(days=1)
            walk_forward = WalkForwardValidator(
                initial_cash=initial_cash,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
                min_positive_windows=self.policy.min_positive_windows,
            ).evaluate(
                events,
                candidate_strategy,
                mark_price,
                start=start,
                end=end,
                train_days=self.policy.train_days,
                test_days=self.policy.test_days,
                holdout_days=self.policy.holdout_days,
            )
            if not walk_forward.passed:
                return ValidationResult(
                    False, walk_forward.reason, candidate, champion_result, walk_forward
                )

        return ValidationResult(
            True,
            "candidate passed backtest and validation gates",
            candidate,
            champion_result,
            walk_forward,
        )
