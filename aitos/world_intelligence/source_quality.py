"""Advisory source-quality and coordinated-burst diagnostics.

These deterministic heuristics identify suspicious ingestion patterns; they do
not label an author as a bot, establish factual truth, or auto-change trust.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import combinations
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
            raise ValueError(
                "source identity and timezone-aware label time are required"
            )


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


@dataclass(frozen=True, slots=True)
class CoordinationEdge:
    source_a: str
    source_b: str
    shared_story_ids: tuple[str, ...]
    max_time_gap_seconds: float


@dataclass(frozen=True, slots=True)
class CoordinatedAmplificationCluster:
    cluster_id: str
    source_ids: tuple[str, ...]
    publisher_groups: tuple[str, ...]
    edges: tuple[CoordinationEdge, ...]
    shared_story_ids: tuple[str, ...]
    max_time_gap_seconds: float
    reasons: tuple[str, ...]


def detect_coordinated_amplification(
    events: Sequence[IntelligenceEvent],
    *,
    as_of: datetime,
    publisher_groups: dict[str, str] | None = None,
    synchronization_window_seconds: float = 180.0,
    lookback_hours: float = 24.0,
    min_shared_stories: int = 2,
    min_sources: int = 3,
    max_sources_per_story: int = 50,
) -> tuple[CoordinatedAmplificationCluster, ...]:
    """Build a conservative source-overlap graph for analyst review.

    Edges require repeated, near-synchronous exact-title observations from
    different operator-defined publisher groups. This detects coordination
    candidates, not bots, authorship, collusion, or factual inaccuracy.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if (
        not isfinite(float(synchronization_window_seconds))
        or synchronization_window_seconds <= 0
        or not isfinite(float(lookback_hours))
        or not 0 < lookback_hours <= 24 * 7
        or isinstance(min_shared_stories, bool)
        or min_shared_stories < 1
        or isinstance(min_sources, bool)
        or min_sources < 3
        or isinstance(max_sources_per_story, bool)
        or not min_sources <= max_sources_per_story <= 500
    ):
        raise ValueError("invalid coordination graph policy")

    cutoff = as_of.astimezone(timezone.utc)
    lower_bound = cutoff - timedelta(hours=lookback_hours)
    groups = dict(publisher_groups or {})
    stories: dict[str, dict[str, list[datetime]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for event in events:
        observed = event.first_seen_at.astimezone(timezone.utc)
        published = event.published_at.astimezone(timezone.utc)
        if observed > cutoff or published > cutoff or observed < lower_bound:
            continue
        normalized = _normalized_title(event.title)
        # Avoid creating edges from short/generic headlines such as "Market update".
        if len(normalized.split()) < 5:
            continue
        stories[normalized][event.source.source_id].append(observed)

    pair_stories: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for normalized_title, source_times in stories.items():
        source_ids = sorted(source_times)
        # Bound pairwise graph expansion for pathological syndication bursts.
        if len(source_ids) > max_sources_per_story:
            continue
        for source_a, source_b in combinations(source_ids, 2):
            group_a = groups.get(source_a, source_a)
            group_b = groups.get(source_b, source_b)
            if not group_a or not group_b or group_a == group_b:
                continue
            time_gap = min(
                abs((time_a - time_b).total_seconds())
                for time_a in source_times[source_a]
                for time_b in source_times[source_b]
            )
            if time_gap <= synchronization_window_seconds:
                story_id = hashlib.sha256(
                    normalized_title.encode("utf-8")
                ).hexdigest()[:16]
                pair_stories[(source_a, source_b)][story_id] = time_gap

    edges: list[CoordinationEdge] = []
    adjacency: dict[str, set[str]] = defaultdict(set)
    for (source_a, source_b), shared in sorted(pair_stories.items()):
        if len(shared) < min_shared_stories:
            continue
        edges.append(
            CoordinationEdge(
                source_a=source_a,
                source_b=source_b,
                shared_story_ids=tuple(sorted(shared)),
                max_time_gap_seconds=max(shared.values()),
            )
        )
        adjacency[source_a].add(source_b)
        adjacency[source_b].add(source_a)

    clusters: list[CoordinatedAmplificationCluster] = []
    unseen = set(adjacency)
    while unseen:
        seed = min(unseen)
        stack = [seed]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            unseen.discard(current)
            stack.extend(adjacency[current] - component)
        if len(component) < min_sources:
            continue
        component_edges = tuple(
            edge
            for edge in edges
            if edge.source_a in component and edge.source_b in component
        )
        shared_story_ids = tuple(
            sorted(
                {
                    story_id
                    for edge in component_edges
                    for story_id in edge.shared_story_ids
                }
            )
        )
        if len(shared_story_ids) < min_shared_stories:
            continue
        source_ids = tuple(sorted(component))
        publisher_ids = tuple(sorted({groups.get(item, item) for item in source_ids}))
        fingerprint = "|".join((*source_ids, *shared_story_ids))
        cluster_id = "coord:" + hashlib.sha256(
            fingerprint.encode("utf-8")
        ).hexdigest()[:16]
        clusters.append(
            CoordinatedAmplificationCluster(
                cluster_id=cluster_id,
                source_ids=source_ids,
                publisher_groups=publisher_ids,
                edges=component_edges,
                shared_story_ids=shared_story_ids,
                max_time_gap_seconds=max(
                    edge.max_time_gap_seconds for edge in component_edges
                ),
                reasons=("repeated_near_synchronous_cross_publisher_story_overlap",),
            )
        )
    return tuple(sorted(clusters, key=lambda item: item.cluster_id))


__all__ = [
    "SourceQualityAssessment",
    "SourceOutcome",
    "SourceReliabilityEstimate",
    "CoordinationEdge",
    "CoordinatedAmplificationCluster",
    "assess_source_batch",
    "estimate_source_reliability",
    "detect_coordinated_amplification",
]
