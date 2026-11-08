"""Deterministic, read-only record-set comparison for storage reconciliation."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable
from uuid import UUID



_TIME_FIELDS = frozenset(
    {
        "time",
        "event_time",
        "ingest_time",
        "timestamp",
        "open_time",
        "close_time",
        "funding_time",
        "created_at",
        "recorded_at",
        "entry_time",
        "exit_time",
    }
)


def _normalize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_value(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _normalize_value(value.value, field_name=field_name)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str) and field_name in _TIME_FIELDS:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return _normalize_datetime(parsed)
    if field_name == "schema_version" and isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and field_name == "schema_version":
        try:
            return float(value)
        except ValueError:
            return value
    return value


def canonical_record_fingerprint(
    record_type: str,
    entity_id: str,
    payload: dict[str, Any],
    *,
    excluded_payload_fields: frozenset[str] = frozenset(),
) -> str:
    """Hash stable record identity and payload, excluding volatile metadata."""
    stable_payload = _normalize_value(
        {
            key: value
            for key, value in payload.items()
            if key not in excluded_payload_fields
        }
    )
    canonical = json.dumps(
        {
            "record_type": str(record_type),
            "entity_id": str(entity_id),
            "payload": stable_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReconciliationResult:
    source_count: int
    target_count: int
    matched_count: int
    source_only: tuple[str, ...]
    target_only: tuple[str, ...]
    source_duplicate_fingerprints: tuple[str, ...]
    target_duplicate_fingerprints: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return (
            not self.source_only
            and not self.target_only
            and not self.source_duplicate_fingerprints
            and not self.target_duplicate_fingerprints
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "clean": self.clean}


def compare_fingerprints(
    source: Iterable[str], target: Iterable[str]
) -> ReconciliationResult:
    """Compare multisets in both directions; duplicate rows remain visible."""
    source_counts = Counter(source)
    target_counts = Counter(target)
    matched = sum((source_counts & target_counts).values())
    source_only = tuple(sorted((source_counts - target_counts).elements()))
    target_only = tuple(sorted((target_counts - source_counts).elements()))
    source_duplicates = tuple(
        sorted(fingerprint for fingerprint, count in source_counts.items() if count > 1)
    )
    target_duplicates = tuple(
        sorted(fingerprint for fingerprint, count in target_counts.items() if count > 1)
    )
    return ReconciliationResult(
        source_count=sum(source_counts.values()),
        target_count=sum(target_counts.values()),
        matched_count=matched,
        source_only=source_only,
        target_only=target_only,
        source_duplicate_fingerprints=source_duplicates,
        target_duplicate_fingerprints=target_duplicates,
    )


def compare_records(
    source: Iterable[dict[str, Any]],
    target: Iterable[dict[str, Any]],
    *,
    excluded_payload_fields: frozenset[str] = frozenset(),
) -> ReconciliationResult:
    def fingerprints(records: Iterable[dict[str, Any]]) -> list[str]:
        return [
            canonical_record_fingerprint(
                row["record_type"],
                row["entity_id"],
                row["payload"],
                excluded_payload_fields=excluded_payload_fields,
            )
            for row in records
        ]

    return compare_fingerprints(fingerprints(source), fingerprints(target))
