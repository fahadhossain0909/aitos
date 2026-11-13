"""Bounded event enrichment pipeline; provenance checks are not fact verification."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping

from aitos.world_intelligence.contracts import IntelligenceEvent
from aitos.world_intelligence.entity_resolution import (
    DEFAULT_ENTITY_REGISTRY,
    EntityAliasRegistry,
)
from aitos.world_intelligence.narratives import NarrativeCluster, cluster_narratives
from aitos.world_intelligence.verification import (
    VerificationResult,
    verify_observations,
)


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    events: tuple[IntelligenceEvent, ...]
    narratives: tuple[NarrativeCluster, ...]
    verification: Mapping[str, VerificationResult]
    as_of: datetime


def enrich_event_batch(
    events: list[IntelligenceEvent] | tuple[IntelligenceEvent, ...],
    *,
    publisher_groups: Mapping[str, str],
    as_of: datetime | None = None,
    registry: EntityAliasRegistry = DEFAULT_ENTITY_REGISTRY,
    max_event_age_hours: float = 72.0,
) -> EnrichmentResult:
    timestamp = (as_of or datetime.now(timezone.utc))
    if timestamp.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if max_event_age_hours <= 0:
        raise ValueError("max_event_age_hours must be positive")
    cutoff = timestamp.astimezone(timezone.utc)
    eligible = [
        event for event in events
        if event.published_at <= cutoff and event.first_seen_at <= cutoff
        and (cutoff - event.first_seen_at).total_seconds() <= max_event_age_hours * 3600
    ]
    enriched = [
        replace(
            event,
            entities=tuple(
                sorted(set(event.entities) | set(registry.resolve(event.title)))
            ),
        )
        for event in eligible
    ]
    narratives = cluster_narratives(enriched, as_of=cutoff)
    narrative_for_event = {
        event_id: narrative.narrative_id
        for narrative in narratives for event_id in narrative.event_ids
    }
    groups: dict[str, list[IntelligenceEvent]] = {}
    for event in enriched:
        group_key = narrative_for_event.get(event.event_id, event.event_id)
        groups.setdefault(group_key, []).append(event)

    results: dict[str, VerificationResult] = {}
    output: list[IntelligenceEvent] = []
    for key, group in groups.items():
        result = verify_observations(
            group, publisher_groups=publisher_groups, now=cutoff
        )
        results[key] = result
        narrative_ids = {item.narrative_id for item in narratives}
        narrative_id = key if key in narrative_ids else None
        for event in group:
            status = event.verification
            if status not in {"disputed", "retracted"}:
                if result.status == "primary_source":
                    status = (
                        "primary_source"
                        if event.source.source_id in result.accepted_source_ids
                        and event.source.is_primary
                        else "unverified"
                    )
                else:
                    status = result.status
            output.append(
                replace(
                    event,
                    verification=status,
                    narrative_ids=tuple(
                        sorted(
                            set(event.narrative_ids)
                            | ({narrative_id} if narrative_id else set())
                        )
                    ),
                )
            )
    output.sort(key=lambda item: (item.first_seen_at, item.event_id))
    return EnrichmentResult(tuple(output), narratives, results, cutoff)


__all__ = ["EnrichmentResult", "enrich_event_batch"]
