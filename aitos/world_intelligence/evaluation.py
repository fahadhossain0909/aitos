"""Chronological, cost-aware evaluation utilities for WIATS research.

Metrics are descriptive diagnostics only. This module does not train, promote,
or activate a model and makes no profitability claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite, log
from typing import Sequence


@dataclass(frozen=True, slots=True)
class EventPrediction:
    event_id: str
    asset: str
    horizon: str
    event_time: datetime
    outcome_available_at: datetime
    predicted_probability_up: float
    forward_return: float
    transaction_cost_bps: float = 0.0
    funding_cost_bps: float = 0.0
    latency_slippage_bps: float = 0.0
    regime: str = "unknown"

    def __post_init__(self) -> None:
        if (
            not self.event_id.strip()
            or not self.asset.strip()
            or not self.horizon.strip()
        ):
            raise ValueError("event_id, asset and horizon are required")
        for name in ("event_time", "outcome_available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.outcome_available_at < self.event_time:
            raise ValueError("outcome cannot be available before the event")
        for name, low, high in (
            ("predicted_probability_up", 0.0, 1.0),
            ("forward_return", -1.0, 1.0),
            ("transaction_cost_bps", 0.0, 100_000.0),
            ("funding_cost_bps", -100_000.0, 100_000.0),
            ("latency_slippage_bps", 0.0, 100_000.0),
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} is outside its finite bounds")
            object.__setattr__(self, name, value)

    @property
    def net_return(self) -> float:
        costs = (
            self.transaction_cost_bps
            + self.funding_cost_bps
            + self.latency_slippage_bps
        ) / 10_000.0
        direction = 1.0 if self.predicted_probability_up >= 0.5 else -1.0
        return direction * self.forward_return - costs


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    sample_count: int
    brier_score: float | None
    log_loss: float | None
    directional_accuracy: float | None
    mean_net_return: float | None
    net_hit_rate: float | None
    total_net_return: float | None


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train: tuple[EventPrediction, ...]
    test: tuple[EventPrediction, ...]
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True, slots=True)
class AblationComparison:
    baseline: EvaluationMetrics
    with_intelligence: EvaluationMetrics
    incremental_mean_net_return: float | None
    matched_sample_count: int


def evaluate_predictions(
    predictions: Sequence[EventPrediction], *, as_of: datetime
) -> EvaluationMetrics:
    """Evaluate only labels fully available at as_of, including execution costs."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    eligible = [
        item
        for item in predictions
        if item.event_time.astimezone(timezone.utc) <= cutoff
        and item.outcome_available_at.astimezone(timezone.utc) <= cutoff
    ]
    if not eligible:
        return EvaluationMetrics(0, None, None, None, None, None, None)
    eps = 1e-12
    brier = sum(
        (item.predicted_probability_up - float(item.forward_return > 0.0)) ** 2
        for item in eligible
    ) / len(eligible)
    log_loss = -sum(
        float(item.forward_return > 0.0) * log(max(eps, item.predicted_probability_up))
        + float(item.forward_return <= 0.0)
        * log(max(eps, 1.0 - item.predicted_probability_up))
        for item in eligible
    ) / len(eligible)
    net = [item.net_return for item in eligible]
    correct = [
        (item.predicted_probability_up >= 0.5) == (item.forward_return > 0.0)
        for item in eligible
    ]
    return EvaluationMetrics(
        sample_count=len(eligible),
        brier_score=brier,
        log_loss=log_loss,
        directional_accuracy=sum(correct) / len(correct),
        mean_net_return=sum(net) / len(net),
        net_hit_rate=sum(value > 0.0 for value in net) / len(net),
        total_net_return=sum(net),
    )


def chronological_walk_forward_splits(
    observations: Sequence[EventPrediction],
    *,
    min_train_samples: int = 100,
    test_size: int = 25,
    embargo: timedelta = timedelta(0),
) -> tuple[WalkForwardSplit, ...]:
    """Create expanding chronological folds with label-availability purging."""
    if min_train_samples < 2 or test_size < 1 or embargo < timedelta(0):
        raise ValueError("invalid walk-forward split policy")
    ordered = sorted(
        observations,
        key=lambda item: (item.event_time.astimezone(timezone.utc), item.event_id),
    )
    splits: list[WalkForwardSplit] = []
    test_start_index = min_train_samples
    while test_start_index < len(ordered):
        test = ordered[test_start_index : test_start_index + test_size]
        if not test:
            break
        start = test[0].event_time.astimezone(timezone.utc)
        end = test[-1].event_time.astimezone(timezone.utc)
        train = tuple(
            item
            for item in ordered[:test_start_index]
            if item.event_time.astimezone(timezone.utc) < start
            and item.outcome_available_at.astimezone(timezone.utc) <= start - embargo
        )
        if len(train) >= min_train_samples:
            splits.append(WalkForwardSplit(tuple(train), tuple(test), start, end))
        test_start_index += test_size
    return tuple(splits)


def compare_ablation(
    baseline: Sequence[EventPrediction],
    with_intelligence: Sequence[EventPrediction],
    *,
    as_of: datetime,
) -> AblationComparison:
    """Compare matched events so the enriched model cannot win by dropping cases."""
    baseline_map = {item.event_id: item for item in baseline}
    enriched_map = {item.event_id: item for item in with_intelligence}
    common = sorted(set(baseline_map) & set(enriched_map))
    base_items = [baseline_map[key] for key in common]
    enriched_items = [enriched_map[key] for key in common]
    for base_item, enriched_item in zip(base_items, enriched_items):
        fixed_fields = (
            "asset",
            "horizon",
            "event_time",
            "outcome_available_at",
            "forward_return",
            "transaction_cost_bps",
            "funding_cost_bps",
            "latency_slippage_bps",
        )
        if any(
            getattr(base_item, field) != getattr(enriched_item, field)
            for field in fixed_fields
        ):
            raise ValueError(
                "ablation rows must share identical outcomes and execution costs"
            )
    base_metrics = evaluate_predictions(base_items, as_of=as_of)
    enriched_metrics = evaluate_predictions(enriched_items, as_of=as_of)
    delta = None
    if (
        base_metrics.mean_net_return is not None
        and enriched_metrics.mean_net_return is not None
    ):
        delta = enriched_metrics.mean_net_return - base_metrics.mean_net_return
    return AblationComparison(base_metrics, enriched_metrics, delta, len(common))


__all__ = [
    "EventPrediction",
    "EvaluationMetrics",
    "WalkForwardSplit",
    "AblationComparison",
    "evaluate_predictions",
    "chronological_walk_forward_splits",
    "compare_ablation",
]
