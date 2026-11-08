"""Multiple-testing and Monte-Carlo validation for Financial ML experiments."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Mapping

from aitos.statistics.selection import (
    AdjustedPValues,
    BootstrapTest,
    ModelConfidenceSet,
    correct_multiple_testing,
    model_confidence_set,
    white_reality_check,
)


@dataclass(frozen=True)
class MonteCarloStressReport:
    paths: int
    horizon: int
    probability_of_loss: float
    median_terminal_return: float
    terminal_return_p05: float
    terminal_return_p95: float
    expected_shortfall_5pct: float
    max_drawdown_p95: float
    additional_cost_bps: float
    block_length: int


@dataclass(frozen=True)
class StrategyFamilyValidation:
    family_size: int
    reality_check: BootstrapTest
    adjusted_p_values: AdjustedPValues | None
    model_confidence_set: ModelConfidenceSet | None


def _finite_series(values: Iterable[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < 20 or any(not math.isfinite(value) for value in result):
        raise ValueError("at least 20 finite out-of-sample returns are required")
    if any(value <= -1.0 for value in result):
        raise ValueError("returns must be greater than -100%")
    return result


def _quantile(values: tuple[float, ...], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def monte_carlo_stress_test(
    returns: Iterable[float],
    *,
    paths: int = 1000,
    horizon: int | None = None,
    block_length: int = 5,
    seed: int = 0,
    additional_cost_bps: float = 0.0,
) -> MonteCarloStressReport:
    """Circular-block bootstrap of net returns with equity and drawdown paths.

    The input must be chronological out-of-sample net returns. Additional
    costs are charged per simulated observation, making this a conservative
    cost-sensitivity overlay while preserving short-range serial dependence.
    """
    values = _finite_series(returns)
    horizon = len(values) if horizon is None else int(horizon)
    if paths < 100 or horizon < 1 or not 1 <= block_length <= len(values):
        raise ValueError(
            "paths >= 100, positive horizon and valid block length required"
        )
    if not math.isfinite(additional_cost_bps) or additional_cost_bps < 0:
        raise ValueError("additional_cost_bps must be finite and non-negative")
    cost = additional_cost_bps / 10_000.0
    rng = random.Random(seed)
    terminal: list[float] = []
    drawdowns: list[float] = []
    for _ in range(paths):
        sampled: list[float] = []
        while len(sampled) < horizon:
            start = rng.randrange(len(values))
            sampled.extend(
                values[(start + offset) % len(values)] for offset in range(block_length)
            )
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for raw_return in sampled[:horizon]:
            net_return = max(-0.999999, raw_return - cost)
            equity *= 1.0 + net_return
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, 1.0 - equity / peak)
        terminal.append(equity - 1.0)
        drawdowns.append(max_drawdown)
    p05 = _quantile(tuple(terminal), 0.05)
    tail = [value for value in terminal if value <= p05]
    return MonteCarloStressReport(
        paths=paths,
        horizon=horizon,
        probability_of_loss=sum(value < 0 for value in terminal) / paths,
        median_terminal_return=_quantile(tuple(terminal), 0.50),
        terminal_return_p05=p05,
        terminal_return_p95=_quantile(tuple(terminal), 0.95),
        expected_shortfall_5pct=mean(tail) if tail else p05,
        max_drawdown_p95=_quantile(tuple(drawdowns), 0.95),
        additional_cost_bps=additional_cost_bps,
        block_length=block_length,
    )


def validate_strategy_family(
    strategy_returns: Mapping[str, Iterable[float]],
    *,
    hypothesis_p_values: Mapping[str, float] | None = None,
    model_losses: Mapping[str, Iterable[float]] | None = None,
    alpha: float = 0.05,
    resamples: int = 1000,
    block_length: int = 5,
    seed: int = 0,
) -> StrategyFamilyValidation:
    """Run White Reality Check, BH-FDR and optional model-confidence-set tests."""
    if len(strategy_returns) < 2:
        raise ValueError("at least two aligned strategy return series are required")
    if not math.isfinite(alpha) or not 0.0 < alpha < 0.5:
        raise ValueError("alpha must be finite and in (0,0.5)")
    reality = white_reality_check(
        strategy_returns,
        resamples=resamples,
        block_length=block_length,
        seed=seed,
    )
    adjusted = (
        correct_multiple_testing(hypothesis_p_values, alpha=alpha, method="bh")
        if hypothesis_p_values
        else None
    )
    confidence_set = (
        model_confidence_set(
            model_losses,
            confidence=1.0 - alpha,
            resamples=resamples,
            block_length=block_length,
            seed=seed,
        )
        if model_losses and len(model_losses) >= 2
        else None
    )
    return StrategyFamilyValidation(
        family_size=len(strategy_returns),
        reality_check=reality,
        adjusted_p_values=adjusted,
        model_confidence_set=confidence_set,
    )
