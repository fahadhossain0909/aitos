"""Conservative source verification with explicit publisher independence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Mapping, Sequence

from aitos.world_intelligence.contracts import IntelligenceEvent


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str
    verified: bool
    independent_source_count: int
    accepted_source_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def verify_observations(
    observations: Sequence[IntelligenceEvent],
    *,
    publisher_groups: Mapping[str, str],
    now: datetime | None = None,
    max_age_seconds: float = 3_600.0,
    min_source_reliability: float = 0.65,
    primary_source_reliability: float = 0.9,
    min_independent_sources: int = 2,
) -> VerificationResult:
    """Assess provenance, freshness and independently configured publishers.

    Publisher groups are operator-maintained trust metadata. Two feeds owned by
    the same publisher count once, preventing syndicated copies from appearing
    as independent confirmation. The function does not decide whether claims
    are factually true; it returns a conservative provenance status only.
    """
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if (
        not isfinite(float(max_age_seconds))
        or max_age_seconds <= 0
        or not 0.0 <= min_source_reliability <= 1.0
        or not 0.0 <= primary_source_reliability <= 1.0
        or min_independent_sources < 2
    ):
        raise ValueError("invalid verification policy")

    if not observations:
        return VerificationResult(
            "unverified", False, 0, (), ("no_observations",)
        )

    if any(event.verification == "retracted" for event in observations):
        return VerificationResult(
            "retracted", False, 0, (), ("retraction_requires_manual_resolution",)
        )
    if any(event.verification == "disputed" for event in observations):
        return VerificationResult(
            "disputed", False, 0, (), ("conflicting_evidence_requires_review",)
        )

    accepted: dict[str, IntelligenceEvent] = {}
    for event in observations:
        source = event.source
        group = publisher_groups.get(source.source_id)
        if not group or not group.strip():
            continue
        age = (
            timestamp.astimezone(timezone.utc) - event.first_seen_at
        ).total_seconds()
        if age < -5.0 or age > max_age_seconds:
            continue
        if source.reliability < min_source_reliability:
            continue
        accepted[source.source_id] = event

    primary = [
        event
        for event in accepted.values()
        if event.source.is_primary
        and event.source.reliability >= primary_source_reliability
    ]
    if primary:
        return VerificationResult(
            "primary_source",
            True,
            1,
            tuple(sorted({event.source.source_id for event in primary})),
            ("fresh_high_reliability_primary_source",),
        )

    groups = {
        publisher_groups[event.source.source_id].strip().casefold()
        for event in accepted.values()
    }
    if len(groups) >= min_independent_sources:
        return VerificationResult(
            "multi_source_confirmed",
            True,
            len(groups),
            tuple(sorted(accepted)),
            ("fresh_independent_publisher_groups",),
        )
    if accepted:
        return VerificationResult(
            "single_source",
            False,
            len(groups),
            tuple(sorted(accepted)),
            ("independent_confirmation_not_reached",),
        )
    return VerificationResult(
        "unverified",
        False,
        0,
        (),
        ("no_fresh_reliable_independent_observation",),
    )


__all__ = ["VerificationResult", "verify_observations"]
