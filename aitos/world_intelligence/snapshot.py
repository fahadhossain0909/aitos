"""Point-in-time asset intelligence snapshot aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp, fsum, isfinite
from typing import Mapping, Sequence

from aitos.world_intelligence.contracts import AssetImpact, IntelligenceEvent
from aitos.world_intelligence.event_study import EventBetaEstimate
from aitos.world_intelligence.governor import (
    IntelligenceRiskGovernor,
    RiskAssessment,
)
from aitos.world_intelligence.impact import estimate_asset_impact


@dataclass(frozen=True, slots=True)
class HorizonImpact:
    horizon: str
    signed_impact: float
    confidence: float
    evidence_count: int


@dataclass(frozen=True, slots=True)
class AssetIntelligenceSnapshot:
    asset: str
    as_of: datetime
    event_count: int
    verified_event_count: int
    unverified_event_count: int
    attention_score: float
    horizon_impacts: tuple[HorizonImpact, ...]
    risk_assessment: RiskAssessment


def build_asset_intelligence_snapshot(
    events: Sequence[IntelligenceEvent],
    *,
    asset: str,
    as_of: datetime,
    beta_estimates: Mapping[tuple[str, str], EventBetaEstimate] | None = None,
    horizons: Mapping[str, float] | None = None,
    governor: IntelligenceRiskGovernor | None = None,
    attention_half_life_hours: float = 6.0,
) -> AssetIntelligenceSnapshot:
    """Aggregate only observations and beta estimates available at as_of.

    This is a compact evidence snapshot, not a return forecast or trade signal.
    It does not mutate strategy/risk state and never invents a missing beta.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not asset or not asset.strip():
        raise ValueError("asset is required")
    if not isfinite(float(attention_half_life_hours)) or attention_half_life_hours <= 0:
        raise ValueError("attention half-life must be positive and finite")
    selected_horizons = dict(horizons or {"1h": 1.0, "4h": 4.0, "24h": 24.0})
    if not selected_horizons or any(
        not isinstance(name, str)
        or not name.strip()
        or not isfinite(float(hours))
        or hours <= 0
        for name, hours in selected_horizons.items()
    ):
        raise ValueError("horizons must contain positive named durations")

    cutoff = as_of.astimezone(timezone.utc)
    point_in_time_events = {
        event.event_id: event
        for event in events
        if event.published_at <= cutoff and event.first_seen_at <= cutoff
    }
    ordered_events = sorted(
        point_in_time_events.values(),
        key=lambda event: (event.first_seen_at, event.event_id),
    )
    verified_events = [
        event
        for event in ordered_events
        if event.verification in {"primary_source", "multi_source_confirmed"}
    ]
    unverified_count = len(ordered_events) - len(verified_events)

    beta_map = beta_estimates or {}
    horizon_results: list[HorizonImpact] = []
    for horizon, hours in selected_horizons.items():
        impacts: list[AssetImpact] = []
        for event in verified_events:
            estimate = beta_map.get((event.event_type, horizon))
            beta = 0.0
            if (
                estimate is not None
                and estimate.status == "estimated"
                and estimate.as_of.tzinfo is not None
                and estimate.as_of.astimezone(timezone.utc) <= cutoff
            ):
                beta = estimate.beta
            impacts.append(
                estimate_asset_impact(
                    event,
                    asset=asset,
                    horizon_hours=hours,
                    beta=beta,
                    now=cutoff,
                )
            )
        weighted = [
            (item.signed_impact, item.confidence)
            for item in impacts
            if item.confidence > 0.0
        ]
        total_weight = fsum(weight for _, weight in weighted)
        aggregate = (
            fsum(value * weight for value, weight in weighted) / total_weight
            if total_weight
            else 0.0
        )
        confidence = min(
            1.0,
            fsum(item.confidence for item in impacts) / max(1, len(impacts)),
        )
        horizon_results.append(
            HorizonImpact(
                horizon=horizon,
                signed_impact=max(-1.0, min(1.0, aggregate)),
                confidence=confidence,
                evidence_count=sum(item.confidence > 0.0 for item in impacts),
            )
        )

    attention = (
        fsum(
            event.severity
            * event.novelty
            * event.confidence
            * event.source.reliability
            * exp(
                -0.6931471805599453
                * max(0.0, (cutoff - event.published_at).total_seconds() / 3600.0)
                / attention_half_life_hours
            )
            for event in ordered_events
        )
        / len(ordered_events)
        if ordered_events
        else 0.0
    )
    attention = max(0.0, min(1.0, attention))

    risk_engine = governor or IntelligenceRiskGovernor()
    fresh_verified_events = [
        event
        for event in verified_events
        if 0.0
        <= (cutoff - event.first_seen_at).total_seconds()
        <= risk_engine.max_age_seconds
    ]
    risk_candidates = fresh_verified_events or verified_events or ordered_events
    risk_event = max(
        risk_candidates,
        key=lambda event: (event.severity, event.first_seen_at),
        default=None,
    )
    if risk_event is None:
        assessment = risk_engine.assess(
            available=False,
            observed_at=None,
            verified=False,
            severity=None,
            now=cutoff,
        )
    else:
        assessment = risk_engine.assess(
            available=True,
            observed_at=risk_event.first_seen_at,
            verified=risk_event in verified_events,
            severity=risk_event.severity,
            uncertainty=1.0 - risk_event.confidence,
            now=cutoff,
        )

    return AssetIntelligenceSnapshot(
        asset=asset.upper(),
        as_of=cutoff,
        event_count=len(ordered_events),
        verified_event_count=len(verified_events),
        unverified_event_count=unverified_count,
        attention_score=attention,
        horizon_impacts=tuple(horizon_results),
        risk_assessment=assessment,
    )


__all__ = [
    "HorizonImpact",
    "AssetIntelligenceSnapshot",
    "build_asset_intelligence_snapshot",
]
