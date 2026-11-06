"""Leakage-aware validation gate around the canonical backtest engine."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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

    def __post_init__(self) -> None:
        if self.train_days < 1 or self.test_days < 1 or self.holdout_days < 1:
            raise ValueError(
                "walk-forward train/test/holdout durations must be positive"
            )
        if self.min_trades < 0 or self.min_positive_windows < 1:
            raise ValueError("validation sample thresholds are invalid")
        if not 0 <= self.max_drawdown <= 1:
            raise ValueError("max_drawdown must be in [0, 1]")


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    reason: str
    candidate: BacktestResult
    champion: BacktestResult | None = None
    walk_forward: WalkForwardResult | None = None


class CandidateValidator:
    """Evaluate frozen candidates without allowing locked holdout leakage.

    Candidate-level threshold/champion comparisons use development events
    only. Sequential test windows and the final locked holdout are evaluated
    separately by WalkForwardValidator and are never fed back into tuning.
    """

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
        events = sorted(list(candidate_events), key=lambda event: event.timestamp)
        if not events:
            raise ValueError("candidate validation requires historical events")
        if self.policy.require_walk_forward:
            if any(
                event.timestamp.tzinfo is None or event.timestamp.utcoffset() is None
                for event in events
            ):
                raise ValueError("walk-forward event timestamps must be timezone-aware")
            start = events[0].timestamp
            last = events[-1].timestamp
            end = last.replace(hour=0, minute=0, second=0, microsecond=0)
            end = end + timedelta(days=1)
            holdout_start = end - timedelta(days=self.policy.holdout_days)
            development_events = [
                event for event in events if event.timestamp < holdout_start
            ]
            if not development_events:
                candidate = BacktestEngine(initial_cash, fee_rate, slippage_bps).run(
                    [], candidate_strategy, mark_price
                )
                return ValidationResult(
                    False,
                    "no development events remain before the locked holdout",
                    candidate,
                    champion_result,
                )
        else:
            start = events[0].timestamp
            end = events[-1].timestamp
            development_events = events

        candidate = BacktestEngine(initial_cash, fee_rate, slippage_bps).run(
            development_events, candidate_strategy, mark_price
        )
        metrics = candidate.metrics
        if metrics.total_return < self.policy.min_total_return:
            return ValidationResult(
                False,
                "candidate return below minimum on development data",
                candidate,
                champion_result,
            )
        if metrics.max_drawdown > self.policy.max_drawdown:
            return ValidationResult(
                False,
                "candidate drawdown above maximum on development data",
                candidate,
                champion_result,
            )
        if metrics.sharpe < self.policy.min_sharpe:
            return ValidationResult(
                False,
                "candidate Sharpe below minimum on development data",
                candidate,
                champion_result,
            )
        if metrics.trades < self.policy.min_trades:
            return ValidationResult(
                False, "insufficient development trades", candidate, champion_result
            )
        if (
            self.policy.require_improvement_over_champion
            and champion_result is not None
        ):
            champion_metrics = champion_result.metrics
            if (
                metrics.total_return <= champion_metrics.total_return
                and metrics.max_drawdown >= champion_metrics.max_drawdown
            ):
                return ValidationResult(
                    False,
                    "candidate does not improve development return or drawdown",
                    candidate,
                    champion_result,
                )

        walk_forward: WalkForwardResult | None = None
        if self.policy.require_walk_forward:
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
            "candidate passed development, walk-forward and locked-holdout gates",
            candidate,
            champion_result,
            walk_forward,
        )
