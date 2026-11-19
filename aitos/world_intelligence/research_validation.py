"""Leakage-aware OOS evaluation, paired benchmarks and confidence intervals."""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt
from typing import Sequence


@dataclass(frozen=True, slots=True)
class OOSPrediction:
    event_id: str
    regime: str
    event_time: datetime
    label_available_at: datetime
    candidate_probability: float
    benchmark_probability: float
    label_up: bool

    def __post_init__(self):
        if not self.event_id.strip() or not self.regime.strip():
            raise ValueError("event and regime identity required")
        for name in ("event_time", "label_available_at"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.label_available_at < self.event_time:
            raise ValueError("label cannot mature before event")
        for name in ("candidate_probability", "benchmark_probability"):
            value = float(getattr(self, name))
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class OOSRegimeReport:
    regime: str
    sample_count: int
    candidate_brier: float
    benchmark_brier: float
    paired_brier_improvement: float
    paired_improvement_ci: tuple[float, float]
    candidate_directional_accuracy: float
    directional_accuracy_ci: tuple[float, float]


@dataclass(frozen=True, slots=True)
class OOSValidationReport:
    as_of: datetime
    reports: tuple[OOSRegimeReport, ...]
    excluded_unmatured: int
    excluded_duplicate_ids: int


def _mean(values):
    return sum(values) / len(values)


def _bootstrap_mean_ci(values, *, seed, iterations, confidence):
    rng = random.Random(seed)
    samples = sorted(_mean([values[rng.randrange(len(values))] for _ in values]) for _ in range(iterations))
    alpha = (1 - confidence) / 2
    return samples[int(alpha * (iterations - 1))], samples[int((1 - alpha) * (iterations - 1))]


def _wilson(successes, count, z=1.959963984540054):
    p = successes / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    margin = z * sqrt((p * (1 - p) / count) + z * z / (4 * count * count)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def evaluate_regime_oos(
    predictions: Sequence[OOSPrediction], *, as_of: datetime,
    training_cutoff: datetime, min_samples: int = 30,
    iterations: int = 2000, confidence: float = 0.95, seed: int = 23,
) -> OOSValidationReport:
    if as_of.tzinfo is None or training_cutoff.tzinfo is None:
        raise ValueError("evaluation and training cutoffs must be timezone-aware")
    if as_of < training_cutoff or min_samples < 3 or iterations < 100 or not 0.5 < confidence < 1:
        raise ValueError("invalid OOS validation policy")
    ids = [p.event_id for p in predictions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate OOS event_id")
    grouped = defaultdict(list)
    excluded = 0
    for row in predictions:
        if row.event_time <= training_cutoff:
            raise ValueError("OOS event must occur strictly after training cutoff")
        if row.event_time > as_of:
            raise ValueError("future event in OOS evaluation")
        if row.label_available_at > as_of:
            excluded += 1
            continue
        grouped[row.regime].append(row)
    reports = []
    for regime, rows in sorted(grouped.items()):
        if len(rows) < min_samples:
            continue
        candidate_losses = [(p.candidate_probability - float(p.label_up)) ** 2 for p in rows]
        benchmark_losses = [(p.benchmark_probability - float(p.label_up)) ** 2 for p in rows]
        improvements = [b - c for b, c in zip(benchmark_losses, candidate_losses)]
        correct = sum((p.candidate_probability >= 0.5) == p.label_up for p in rows)
        reports.append(OOSRegimeReport(
            regime=regime, sample_count=len(rows),
            candidate_brier=_mean(candidate_losses), benchmark_brier=_mean(benchmark_losses),
            paired_brier_improvement=_mean(improvements),
            paired_improvement_ci=_bootstrap_mean_ci(improvements, seed=seed, iterations=iterations, confidence=confidence),
            candidate_directional_accuracy=correct / len(rows),
            directional_accuracy_ci=_wilson(correct, len(rows)),
        ))
    return OOSValidationReport(as_of.astimezone(timezone.utc), tuple(reports), excluded, 0)


__all__ = ["OOSPrediction", "OOSRegimeReport", "OOSValidationReport", "evaluate_regime_oos"]
