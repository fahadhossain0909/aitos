"""Historical provider ingestion with immutable provenance and PIT persistence.

Adapters must implement fetch_page(cursor). A page is persisted before its
checkpoint is advanced; retries are at-least-once and revision identities make
the point-in-time dataset replayable. Raw responses are bounded and hashed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from aitos.world_intelligence.point_in_time import EventRevision, MarketBarRevision
from aitos.world_intelligence.point_in_time_storage import ClickHousePointInTimeStore


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    source_id: str
    source_kind: str
    record_id: str
    uri: str
    published_at: datetime
    known_at: datetime
    raw_payload: bytes
    metadata: dict[str, Any]

    def __post_init__(self):
        for value in (self.source_id, self.source_kind, self.record_id, self.uri):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("source provenance identity and URI are required")
        for name in ("published_at", "known_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.known_at < self.published_at:
            raise ValueError("known_at cannot precede published_at")
        if not isinstance(self.raw_payload, bytes) or len(self.raw_payload) > 512_000:
            raise ValueError("raw_payload must be bytes no larger than 512KB")
        json.dumps(self.metadata, allow_nan=False)


@dataclass(frozen=True, slots=True)
class HistoricalProviderPage:
    source_id: str
    next_cursor: str | None
    event_revisions: tuple[EventRevision, ...]
    market_bar_revisions: tuple[MarketBarRevision, ...]
    provenance: tuple[SourceProvenance, ...]


class HistoricalProvider(Protocol):
    source_id: str

    async def fetch_page(self, *, cursor: str | None) -> HistoricalProviderPage: ...


class HistoricalIngestionStore:
    """Persists raw source provenance plus point-in-time event/bar revisions."""

    def __init__(self, client, *, database: str = "aitos"):
        self.client = client
        self.revisions = ClickHousePointInTimeStore(client, database)
        self.database = database
        self.table = f"{database}.world_intelligence_source_provenance"

    async def ensure_schema(self):
        await self.revisions.ensure_schema()
        await self.client.command(f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                source_id LowCardinality(String),
                source_kind LowCardinality(String),
                record_id String,
                safe_uri String,
                published_at DateTime64(3, 'UTC'),
                known_at DateTime64(3, 'UTC'),
                ingested_at DateTime64(6, 'UTC'),
                payload_sha256 FixedString(64),
                raw_payload String,
                metadata_json String
            )
            ENGINE = ReplacingMergeTree(ingested_at)
            PARTITION BY toYYYYMM(known_at)
            ORDER BY (source_id, record_id, payload_sha256)
        """)

    async def append_provenance(self, records: tuple[SourceProvenance, ...]) -> int:
        keys = [(r.source_id, r.record_id, hashlib.sha256(r.raw_payload).hexdigest()) for r in records]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate source provenance identity in batch")
        now = datetime.now(timezone.utc)
        rows = []
        for record in records:
            parsed = urlsplit(record.uri)
            safe_uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("provenance URI must be HTTP(S)")
            rows.append((
                record.source_id, record.source_kind, record.record_id, safe_uri,
                record.published_at, record.known_at, now,
                hashlib.sha256(record.raw_payload).hexdigest(),
                record.raw_payload.decode("utf-8", errors="replace"),
                json.dumps(record.metadata, sort_keys=True, separators=(",", ":"), allow_nan=False),
            ))
        if rows:
            await self.client.insert(self.table, rows, column_names=[
                "source_id", "source_kind", "record_id", "safe_uri", "published_at",
                "known_at", "ingested_at", "payload_sha256", "raw_payload", "metadata_json",
            ])
        return len(rows)

    async def ingest(self, provider: HistoricalProvider, *, cursor: str | None = None) -> str | None:
        page = await provider.fetch_page(cursor=cursor)
        if page.source_id != provider.source_id:
            raise ValueError("provider/page source identity mismatch")
        if any(item.source_id != page.source_id for item in page.provenance):
            raise ValueError("provenance source identity mismatch")
        # Persist provenance and revisions before returning the next cursor.
        await self.append_provenance(page.provenance)
        await self.revisions.append_event_revisions(list(page.event_revisions))
        await self.revisions.append_market_bar_revisions(list(page.market_bar_revisions))
        return page.next_cursor


__all__ = ["HistoricalIngestionStore", "HistoricalProvider", "HistoricalProviderPage", "SourceProvenance"]
