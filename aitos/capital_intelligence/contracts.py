"""Provider-neutral, versioned capital-flow contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _unit(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be finite and in [0, 1]")
    return value


@dataclass(frozen=True, slots=True)
class EntityEvidence:
    subject_id: str
    object_id: str
    relation: str
    evidence_type: str
    evidence_ref: str
    observed_at: datetime
    confidence: float
    status: str = "candidate"

    def __post_init__(self):
        if not all(
            v.strip()
            for v in (
                self.subject_id,
                self.object_id,
                self.relation,
                self.evidence_type,
                self.evidence_ref,
            )
        ):
            raise ValueError("relationship identity and evidence are required")
        if self.subject_id == self.object_id:
            raise ValueError("self relationship is invalid")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "confidence", _unit(self.confidence, "confidence"))
        if self.status not in {"verified", "probable", "candidate", "rejected"}:
            raise ValueError("invalid relationship status")


@dataclass(frozen=True, slots=True)
class EntityLabel:
    entity_id: str
    label: str
    category: str
    source: str
    verified: bool
    valid_from: datetime
    valid_until: datetime | None = None

    def __post_init__(self):
        if not all(
            v.strip() for v in (self.entity_id, self.label, self.category, self.source)
        ):
            raise ValueError("label fields required")
        start = _utc(self.valid_from)
        end = _utc(self.valid_until) if self.valid_until else None
        if end and end <= start:
            raise ValueError("invalid label validity interval")
        object.__setattr__(self, "valid_from", start)
        object.__setattr__(self, "valid_until", end)


@dataclass(frozen=True, slots=True)
class CapitalEvent:
    event_id: str
    source_id: str
    source_kind: str
    market: str
    venue: str
    instrument: str
    event_type: str
    event_time: datetime
    observed_time: datetime
    quantity: float
    unit: str
    usd_value: float | None
    actor_id: str | None = None
    counterparty_id: str | None = None
    transaction_id: str | None = None
    block_number: int | None = None
    block_hash: str | None = None
    finality: str = "unknown"
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        required = (
            self.event_id,
            self.source_id,
            self.source_kind,
            self.market,
            self.venue,
            self.instrument,
            self.event_type,
            self.unit,
        )
        if not all(v.strip() for v in required):
            raise ValueError("event identity fields are required")
        object.__setattr__(self, "event_time", _utc(self.event_time))
        object.__setattr__(self, "observed_time", _utc(self.observed_time))
        quantity = float(self.quantity)
        if not math.isfinite(quantity) or quantity < 0:
            raise ValueError("quantity must be finite and non-negative")
        object.__setattr__(self, "quantity", quantity)
        if self.usd_value is not None:
            value = float(self.usd_value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("usd_value must be finite and non-negative")
            object.__setattr__(self, "usd_value", value)
        object.__setattr__(self, "confidence", _unit(self.confidence, "confidence"))
        if self.finality not in {
            "unknown",
            "pending",
            "confirmed",
            "finalized",
            "reverted",
        }:
            raise ValueError("invalid finality")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")

    @property
    def deduplication_key(self):
        raw = "|".join((self.source_id, self.event_id, self.transaction_id or ""))
        return hashlib.sha256(raw.encode()).hexdigest()

    def to_dict(self):
        result = asdict(self)
        result["event_time"] = self.event_time.isoformat()
        result["observed_time"] = self.observed_time.isoformat()
        return result

    def to_json(self):
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
