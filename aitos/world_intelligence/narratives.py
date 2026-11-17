"""Conservative lexical narrative clustering with explicit time bounds."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from math import fsum

from aitos.world_intelligence.contracts import IntelligenceEvent

_STOPWORDS = {
    "about",
    "after",
    "amid",
    "and",
    "are",
    "from",
    "has",
    "have",
    "into",
    "its",
    "over",
    "says",
    "that",
    "the",
    "their",
    "this",
    "today",
    "with",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{2,}", text.casefold())
        if token not in _STOPWORDS
    }


def _similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


@dataclass(frozen=True, slots=True)
class NarrativeCluster:
    narrative_id: str
    event_ids: tuple[str, ...]
    entities: tuple[str, ...]
    event_types: tuple[str, ...]
    source_ids: tuple[str, ...]
    first_seen_at: datetime
    last_seen_at: datetime
    mean_annotated_sentiment: float
    confidence: float
    status: str = "unverified_narrative"


def cluster_narratives(
    events: list[IntelligenceEvent] | tuple[IntelligenceEvent, ...],
    *,
    as_of: datetime,
    lookback_hours: float = 24.0,
    min_similarity: float = 0.25,
    max_events: int = 500,
) -> tuple[NarrativeCluster, ...]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if lookback_hours <= 0 or not 0.0 <= min_similarity <= 1.0:
        raise ValueError("invalid narrative clustering policy")
    if not 1 <= max_events <= 5_000:
        raise ValueError("max_events must be between 1 and 5000")
    cutoff = as_of.astimezone(timezone.utc)
    eligible = [
        event
        for event in events
        if event.published_at <= cutoff
        and event.first_seen_at <= cutoff
        and (cutoff - event.first_seen_at).total_seconds() <= lookback_hours * 3600
        and event.entities
    ]
    eligible = sorted(eligible, key=lambda item: (item.first_seen_at, item.event_id))[
        -max_events:
    ]
    tokens = [_tokens(event.title) for event in eligible]
    parents = list(range(len(eligible)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for left, right in combinations(range(len(eligible)), 2):
        if not set(eligible[left].entities).intersection(eligible[right].entities):
            continue
        if _similarity(tokens[left], tokens[right]) < min_similarity:
            continue
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parents[max(root_left, root_right)] = min(root_left, root_right)

    groups: dict[int, list[IntelligenceEvent]] = {}
    for index, event in enumerate(eligible):
        groups.setdefault(find(index), []).append(event)

    clusters: list[NarrativeCluster] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        keys = sorted(event.deduplication_key for event in group)
        narrative_id = hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()
        sentiments = [event.sentiment for event in group]
        clusters.append(
            NarrativeCluster(
                narrative_id=narrative_id,
                event_ids=tuple(sorted(event.event_id for event in group)),
                entities=tuple(
                    sorted({entity for event in group for entity in event.entities})
                ),
                event_types=tuple(sorted({event.event_type for event in group})),
                source_ids=tuple(sorted({event.source.source_id for event in group})),
                first_seen_at=min(event.first_seen_at for event in group),
                last_seen_at=max(event.first_seen_at for event in group),
                mean_annotated_sentiment=fsum(sentiments) / len(sentiments),
                confidence=min(
                    0.55, fsum(event.confidence for event in group) / len(group)
                ),
            )
        )
    return tuple(
        sorted(clusters, key=lambda item: (item.last_seen_at, item.narrative_id))
    )


__all__ = ["NarrativeCluster", "cluster_narratives"]
