"""Conservative asset-specific event impact estimation."""

from __future__ import annotations

from datetime import datetime, timezone
from math import exp, isfinite

from aitos.world_intelligence.contracts import AssetImpact, IntelligenceEvent


def estimate_asset_impact(
    event: IntelligenceEvent,
    *,
    asset: str,
    horizon_hours: float,
    beta: float = 0.0,
    now: datetime | None = None,
    half_life_hours: float = 6.0,
) -> AssetImpact:
    """Create a bounded research feature using a supplied point-in-time beta.

    A missing beta defaults to zero so the model never invents cross-asset links.
    """
    if horizon_hours <= 0 or half_life_hours <= 0:
        raise ValueError("horizons and half-life must be positive")
    if not isfinite(float(beta)) or not -2.0 <= float(beta) <= 2.0:
        raise ValueError("beta must be finite and bounded to [-2, 2]")
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    age_hours = max(
        0.0,
        (timestamp.astimezone(timezone.utc) - event.published_at).total_seconds()
        / 3600.0,
    )
    decay = exp(-0.6931471805599453 * age_hours / half_life_hours)
    verification_factor = {
        "multi_source_confirmed": 1.0,
        "primary_source": 1.0,
        "single_source": 0.65,
        "unverified": 0.0,
        "disputed": 0.0,
        "retracted": 0.0,
    }.get(event.verification, 0.0)
    signed = event.sentiment * event.severity * event.novelty * event.confidence
    signed *= event.source.reliability * verification_factor * float(beta) * decay
    signed = max(-1.0, min(1.0, signed))
    confidence = (
        event.confidence * event.source.reliability * verification_factor * decay
    )
    return AssetImpact(
        event_id=event.event_id,
        asset=asset.upper(),
        horizon=f"{horizon_hours:g}h",
        signed_impact=signed,
        confidence=max(0.0, min(1.0, confidence)),
        evidence_count=1,
        explanation="Bounded event feature; beta must be historically estimated and OOS validated.",
    )
