"""Leakage-safe probability calibration diagnostics for World Intelligence.

This module evaluates already-produced probabilities. It never fits a model,
changes a trading policy, or promotes a candidate.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, log, sqrt


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    observation_id: str
    asset: str
    horizon: str
    regime: str
    predicted_at: datetime
    outcome_available_at: datetime
    probability_up: float
    outcome_up: bool

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.observation_id, self.asset, self.horizon, self.regime)
        ):
            raise ValueError(
                "observation identity, asset, horizon and regime are required"
            )
        for name in ("predicted_at", "outcome_available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.outcome_available_at < self.predicted_at:
            raise ValueError("outcome cannot be available before prediction")
        probability = float(self.probability_up)
        if not isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("probability_up must be finite and in [0, 1]")
        object.__setattr__(self, "probability_up", probability)
        if not isinstance(self.outcome_up, bool):
            raise ValueError("outcome_up must be boolean")


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower_bound: float
    upper_bound: float
    sample_count: int
    mean_probability: float | None
    observed_frequency: float | None
    confidence_low: float | None
    confidence_high: float | None


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    sample_count: int
    excluded_unmatured: int
    brier_score: float | None
    log_loss: float | None
    expected_calibration_error: float | None
    maximum_calibration_error: float | None
    bins: tuple[CalibrationBin, ...]


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054):
    if total <= 0:
        return None, None
    rate = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = (rate + z2 / (2.0 * total)) / denominator
    margin = (
        z * sqrt(rate * (1.0 - rate) / total + z2 / (4.0 * total * total)) / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def calibration_report(
    observations: Sequence[CalibrationObservation],
    *,
    as_of: datetime,
    bin_count: int = 10,
) -> CalibrationReport:
    """Return fixed-width reliability bins using only outcomes known at as_of."""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not 2 <= bin_count <= 50:
        raise ValueError("bin_count must be between 2 and 50")
    seen: set[str] = set()
    for item in observations:
        if item.observation_id in seen:
            raise ValueError(f"duplicate observation_id: {item.observation_id}")
        seen.add(item.observation_id)

    cutoff = as_of.astimezone(timezone.utc)
    eligible = [item for item in observations if item.outcome_available_at <= cutoff]
    excluded = len(observations) - len(eligible)
    if not eligible:
        empty = tuple(
            CalibrationBin(
                index / bin_count,
                (index + 1) / bin_count,
                0,
                None,
                None,
                None,
                None,
            )
            for index in range(bin_count)
        )
        return CalibrationReport(0, excluded, None, None, None, None, empty)

    buckets: list[list[CalibrationObservation]] = [[] for _ in range(bin_count)]
    eps = 1e-12
    brier_sum = 0.0
    log_loss_sum = 0.0
    for item in eligible:
        index = min(int(item.probability_up * bin_count), bin_count - 1)
        buckets[index].append(item)
        label = float(item.outcome_up)
        brier_sum += (item.probability_up - label) ** 2
        p = min(1.0 - eps, max(eps, item.probability_up))
        log_loss_sum -= label * log(p) + (1.0 - label) * log(1.0 - p)

    bins: list[CalibrationBin] = []
    weighted_error = 0.0
    maximum_error = 0.0
    for index, bucket in enumerate(buckets):
        count = len(bucket)
        lower, upper = index / bin_count, (index + 1) / bin_count
        if not count:
            bins.append(CalibrationBin(lower, upper, 0, None, None, None, None))
            continue
        mean_probability = sum(x.probability_up for x in bucket) / count
        observed = sum(x.outcome_up for x in bucket) / count
        low, high = _wilson_interval(sum(x.outcome_up for x in bucket), count)
        error = abs(mean_probability - observed)
        weighted_error += count / len(eligible) * error
        maximum_error = max(maximum_error, error)
        bins.append(
            CalibrationBin(lower, upper, count, mean_probability, observed, low, high)
        )

    total = len(eligible)
    return CalibrationReport(
        sample_count=total,
        excluded_unmatured=excluded,
        brier_score=brier_sum / total,
        log_loss=log_loss_sum / total,
        expected_calibration_error=weighted_error,
        maximum_calibration_error=maximum_error,
        bins=tuple(bins),
    )


def calibration_cohorts(
    observations: Sequence[CalibrationObservation],
    *,
    as_of: datetime,
    bin_count: int = 10,
) -> dict[tuple[str, str, str], CalibrationReport]:
    """Produce independent asset/horizon/regime reports; no cross-cohort pooling."""
    groups: dict[tuple[str, str, str], list[CalibrationObservation]] = defaultdict(list)
    for item in observations:
        key = (item.asset.upper(), item.horizon, item.regime)
        groups[key].append(item)
    return {
        key: calibration_report(group, as_of=as_of, bin_count=bin_count)
        for key, group in sorted(groups.items())
    }


__all__ = [
    "CalibrationBin",
    "CalibrationObservation",
    "CalibrationReport",
    "calibration_cohorts",
    "calibration_report",
]
