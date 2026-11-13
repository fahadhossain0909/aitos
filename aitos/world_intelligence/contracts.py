"""Versioned, dependency-light contracts for external intelligence events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Any, Mapping

SCHEMA_VERSION = 1


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _unit(value: float, name: str) -> float:
    number = float(value)
    if not isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return number


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """Provenance for one observation; syndicated copies are not independent."""

    source_id: str
    source_kind: str
    uri: str
    published_at: datetime
    first_seen_at: datetime
    reliability: float
    is_primary: bool = False
    original_story_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.source_kind.strip():
            raise ValueError("source identity is required")
        object.__setattr__(
            self, "published_at", _utc(self.published_at, "published_at")
        )
        object.__setattr__(
            self, "first_seen_at", _utc(self.first_seen_at, "first_seen_at")
        )
        object.__setattr__(self, "reliability", _unit(self.reliability, "reliability"))
        if self.first_seen_at < self.published_at:
            raise ValueError("first_seen_at cannot precede published_at")


@dataclass(frozen=True, slots=True)
class IntelligenceEvent:
    """Canonical event with bounded model outputs and explicit provenance."""

    event_id: str
    event_type: str
    title: str
    entities: tuple[str, ...]
    published_at: datetime
    first_seen_at: datetime
    source: SourceEvidence
    sentiment: float
    severity: float
    novelty: float
    confidence: float
    verification: str = "unverified"
    impact_horizon: str = "unknown"
    narrative_ids: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not self.event_id.strip()
            or not self.event_type.strip()
            or not self.title.strip()
        ):
            raise ValueError("event identity, type and title are required")
        object.__setattr__(
            self, "published_at", _utc(self.published_at, "published_at")
        )
        object.__setattr__(
            self, "first_seen_at", _utc(self.first_seen_at, "first_seen_at")
        )
        if self.first_seen_at < self.published_at:
            raise ValueError("first_seen_at cannot precede published_at")
        sentiment = float(self.sentiment)
        if not isfinite(sentiment) or not -1.0 <= sentiment <= 1.0:
            raise ValueError("sentiment must be finite and between -1 and 1")
        object.__setattr__(self, "sentiment", sentiment)
        for name in ("severity", "novelty", "confidence"):
            object.__setattr__(self, name, _unit(getattr(self, name), name))
        entities = tuple(
            sorted(
                {
                    str(item).strip().upper()
                    for item in self.entities
                    if str(item).strip()
                }
            )
        )
        object.__setattr__(self, "entities", entities)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")

    @property
    def deduplication_key(self) -> str:
        """Stable source-story identity; not a semantic event-clustering key."""
        origin = self.source.original_story_id or self.source.uri or self.title
        raw = "|".join(
            (self.source.source_id, self.event_type, origin.strip().lower())
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["published_at"] = self.published_at.isoformat()
        result["first_seen_at"] = self.first_seen_at.isoformat()
        result["source"]["published_at"] = self.source.published_at.isoformat()
        result["source"]["first_seen_at"] = self.source.first_seen_at.isoformat()
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )


@dataclass(frozen=True, slots=True)
class AssetImpact:
    """Horizon-specific impact hypothesis; not a return forecast."""

    event_id: str
    asset: str
    horizon: str
    signed_impact: float
    confidence: float
    evidence_count: int
    explanation: str

    def __post_init__(self) -> None:
        value = float(self.signed_impact)
        if not self.event_id.strip() or not self.asset.strip():
            raise ValueError("event_id and asset are required")
        if not isfinite(value) or not -1.0 <= value <= 1.0:
            raise ValueError("signed_impact must be finite and between -1 and 1")
        object.__setattr__(self, "signed_impact", value)
        object.__setattr__(self, "confidence", _unit(self.confidence, "confidence"))
        if self.evidence_count < 1:
            raise ValueError("evidence_count must be positive")
