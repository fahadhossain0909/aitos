"""Advisory source-quality and coordinated-burst diagnostics.

These deterministic heuristics identify suspicious ingestion patterns; they do
not label an author as a bot, establish factual truth, or auto-change trust.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt
from typing import Sequence

from aitos.world_intelligence.contracts import IntelligenceEvent


@dataclass(frozen=True, slots=True)
class SourceQualityAssessment:
    source_id: str
    observation_count: int
    unique_story_count: int
    duplicate_fraction: float
    events_per_minute: float
    suspected_automation: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    source_id: str
    label_available_at: datetime
    is_correct: bool

    def __post_init__(self) -> None:
        if not self.source_id.strip() or self.label_available_at.tzinfo is None:
            raise ValueError("source identity and timezone-aware label time are required")


@dataclass(frozen=True, slots=True)
class SourceReliabilityEstimate:
    source_id: str
    sample_count: int
    confirmed_count: int
    posterior_mean: float
    standard_error: float
    status: str


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def assess_source_batch(
    events: Sequence[IntelligenceEvent],
    *,
    as_of: datetime,
    window_seconds: float = 300.0,
    burst_threshold_per_minute: float = 30.0,
    duplicate_fraction_threshold: float = 0.6,
    min_duplicate_observations: int = 5,
) -> tuple[SourceQualityAssessment, ...]:
    """Flag high-rate or repetitive source batches for analyst review."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if (
        not isfinite(float(window_seconds))
        or window_seconds <= 0
        or burst_threshold_per_minute <= 0
        or not 0.0 <= duplicate_fraction_threshold <= 1.0
        or min_duplicate_observations < 2
    ):
        raise ValueError("invalid source-quality policy")
    cutoff = as_of.astimezone(timezone.utc)
    grouped: dict[str, list[IntelligenceEvent]] = defaultdict(list)
    for event in events:
        age = (cutoff - event.first_seen_at).total_seconds()
        if 0.0 <= age <= window_seconds:
            grouped[event.source.source_id].append(event)

    reports: list[SourceQualityAssessment] = []
    for source_id, batch in grouped.items():
        story_ids = {event.deduplication_key for event in batch}
        titles = Counter(_normalized_title(event.title) for event in batch)
        repeated_title_count = sum(count - 1 for count in titles.values() if count > 1)
        duplicate_fraction = max(
            1.0 - len(story_ids) / len(batch),
            repeated_title_count / len(batch),
        )
        events_per_minute = len(batch) * 60.0 / window_seconds
        reasons: list[str] = []
        if events_per_minute >= burst_threshold_per_minute:
            reasons.append("unusually_high_source_event_rate")
        if (
            len(batch) >= min_duplicate_observations
            and duplicate_fraction >= duplicate_fraction_threshold
        ):
            reasons.append("high_exact_or_story_duplicate_fraction")
        reports.append(
            SourceQualityAssessment(
                source_id=source_id,
                observation_count=len(batch),
                unique_story_count=len(story_ids),
                duplicate_fraction=min(1.0, duplicate_fraction),
                events_per_minute=events_per_minute,
                suspected_automation=bool(reasons),
                reasons=tuple(reasons),
            )
        )
    return tuple(sorted(reports, key=lambda item: item.source_id))


def estimate_source_reliability(
    outcomes: Sequence[SourceOutcome],
    *,
    source_id: str,
    as_of: datetime,
    min_samples: int = 20,
    prior_alpha: float = 2.0,
    prior_beta: float = 2.0,
) -> SourceReliabilityEstimate:
    """Return a point-in-time Beta-Binomial trust estimate; never mutates policy."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if (
        min_samples < 1
        or not isfinite(prior_alpha)
        or not isfinite(prior_beta)
        or prior_alpha <= 0
        or prior_beta <= 0
    ):
        raise ValueError("invalid reliability calibration policy")
    cutoff = as_of.astimezone(timezone.utc)
    eligible = [
        item
        for item in outcomes
        if item.source_id == source_id
        and item.label_available_at.astimezone(timezone.utc) <= cutoff
    ]
    confirmed = sum(item.is_correct for item in eligible)
    alpha = prior_alpha + confirmed
    beta = prior_beta + len(eligible) - confirmed
    total = alpha + beta
    mean = alpha / total
    error = sqrt(alpha * beta / (total * total * (total + 1.0)))
    return SourceReliabilityEstimate(
        source_id=source_id,
        sample_count=len(eligible),
        confirmed_count=confirmed,
        posterior_mean=mean,
        standard_error=error,
        status="calibrated" if len(eligible) >= min_samples else "insufficient_history",
    )


__all__ = [
    "SourceQualityAssessment",
    "SourceOutcome",
    "SourceReliabilityEstimate",
    "assess_source_batch",
    "estimate_source_reliability",
]
