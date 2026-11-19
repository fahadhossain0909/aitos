"""Historical provider ingestion with immutable provenance and PIT persistence.

Adapters must implement fetch_page(cursor). A page is persisted before its
checkpoint is advanced; retries are at-least-once and revision identities make
the point-in-time dataset replayable. Raw responses are bounded and hashed.
"""

from __future__ import annotations

import base64
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


class HistoricalBarProvider(Protocol):
    async def fetch_revisions(
        self, *, assets: tuple[str, ...], start: datetime, end: datetime
    ) -> tuple[MarketBarRevision, ...]: ...


class NormalizedProviderHistoryAdapter:
    """Bridge an existing normalized source adapter into the historical store.

    The event's canonical JSON is retained as the normalized source payload.
    To preserve the provider's exact wire response, implement HistoricalProvider
    directly and supply that response in SourceProvenance.raw_payload.
    """

    def __init__(
        self, source, bar_provider: HistoricalBarProvider, *, asset_resolver, clock=None
    ):
        self.source = source
        self.bar_provider = bar_provider
        self.asset_resolver = asset_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.source_id = source.source_id

    async def fetch_page(self, *, cursor: str | None) -> HistoricalProviderPage:
        events, next_cursor = await self.source.poll(cursor=cursor)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("historical adapter clock must be timezone-aware")
        revisions = []
        provenance = []
        assets = set()
        for event in events:
            asset = self.asset_resolver(event)
            if not asset:
                continue
            assets.add(str(asset).upper())
            payload = event.to_json().encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            revisions.append(
                EventRevision(
                    event_id=event.event_id,
                    revision_id=digest,
                    asset=str(asset).upper(),
                    event_type=event.event_type,
                    published_at=event.published_at,
                    known_at=event.first_seen_at,
                    sentiment=event.sentiment,
                    severity=event.severity,
                    novelty=event.novelty,
                    confidence=event.confidence,
                    source_reliability=event.source.reliability,
                    regime=str(event.metadata.get("regime", "unknown")),
                    beta=float(event.metadata.get("beta", 0.0)),
                )
            )
            provenance.append(
                SourceProvenance(
                    source_id=self.source_id,
                    source_kind=event.source.source_kind,
                    record_id=event.event_id,
                    uri=event.source.uri,
                    published_at=event.source.published_at,
                    known_at=event.first_seen_at,
                    raw_payload=payload,
                    metadata={
                        "payload_kind": "normalized_event_json",
                        "schema_version": event.schema_version,
                        "deduplication_key": event.deduplication_key,
                        "original_story_id": event.source.original_story_id,
                    },
                )
            )
        bars = ()
        if assets:
            bars = await self.bar_provider.fetch_revisions(
                assets=tuple(sorted(assets)),
                start=min(item.known_at for item in revisions),
                end=now,
            )
        return HistoricalProviderPage(
            source_id=self.source_id,
            next_cursor=next_cursor,
            event_revisions=tuple(revisions),
            market_bar_revisions=tuple(bars),
            provenance=tuple(provenance),
        )


class HistoricalIngestionStore:
    """Persists raw source provenance plus point-in-time event/bar revisions."""

    def __init__(self, client, *, database: str = "aitos"):
        self.client = client
        self.revisions = ClickHousePointInTimeStore(client, database)
        self.database = database
        self.table = f"{database}.world_intelligence_source_provenance"
        self.cursors_table = f"{database}.world_intelligence_historical_cursors"

    async def ensure_schema(self):
        await self.revisions.ensure_schema()
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.cursors_table} (
                source_id LowCardinality(String),
                cursor String,
                fencing_token UInt64 DEFAULT 0,
                updated_at DateTime64(6, 'UTC')
            )
            ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY source_id
        """
        )
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                source_id LowCardinality(String),
                source_kind LowCardinality(String),
                record_id String,
                safe_uri String,
                published_at DateTime64(3, 'UTC'),
                known_at DateTime64(3, 'UTC'),
                ingested_at DateTime64(6, 'UTC'),
                payload_sha256 FixedString(64),
                raw_payload_base64 String,
                metadata_json String
            )
            ENGINE = ReplacingMergeTree(ingested_at)
            PARTITION BY toYYYYMM(known_at)
            ORDER BY (source_id, record_id, payload_sha256)
        """
        )

    async def append_provenance(self, records: tuple[SourceProvenance, ...]) -> int:
        keys = [
            (r.source_id, r.record_id, hashlib.sha256(r.raw_payload).hexdigest())
            for r in records
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate source provenance identity in batch")
        now = datetime.now(timezone.utc)
        rows = []
        for record in records:
            parsed = urlsplit(record.uri)
            safe_uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("provenance URI must be HTTP(S)")
            rows.append(
                (
                    record.source_id,
                    record.source_kind,
                    record.record_id,
                    safe_uri,
                    record.published_at,
                    record.known_at,
                    now,
                    hashlib.sha256(record.raw_payload).hexdigest(),
                    base64.b64encode(record.raw_payload).decode("ascii"),
                    json.dumps(
                        record.metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            )
        if rows:
            await self.client.insert(
                self.table,
                rows,
                column_names=[
                    "source_id",
                    "source_kind",
                    "record_id",
                    "safe_uri",
                    "published_at",
                    "known_at",
                    "ingested_at",
                    "payload_sha256",
                    "raw_payload",
                    "metadata_json",
                ],
            )
        return len(rows)

    async def backfill(
        self,
        provider: HistoricalProvider,
        *,
        start_cursor: str | None = None,
        max_pages: int = 1000,
        fencing_token: int = 0,
    ) -> tuple[int, str | None]:
        """Run a bounded provider cursor to exhaustion; date semantics stay in adapter."""
        if isinstance(max_pages, bool) or not 1 <= max_pages <= 100_000:
            raise ValueError("max_pages must be between 1 and 100000")
        cursor = start_cursor
        pages = 0
        for _ in range(max_pages):
            previous = cursor
            cursor = await self.ingest(
                provider, cursor=cursor, fencing_token=fencing_token
            )
            pages += 1
            if cursor is None:
                return pages, None
            if cursor == previous:
                raise RuntimeError("historical provider cursor did not advance")
        raise RuntimeError("historical backfill page limit reached before exhaustion")

    async def load_cursor(self, source_id: str) -> str | None:
        result = await self.client.query(
            f"SELECT argMax(cursor, (fencing_token, updated_at)) FROM {self.cursors_table} "  # nosec B608 - database identifier is validated by ClickHousePointInTimeStore
            "WHERE source_id = {source_id:String}",
            parameters={"source_id": source_id},
        )
        return (result.result_rows[0][0] or None) if result.result_rows else None

    async def save_cursor(
        self, source_id: str, cursor: str | None, *, fencing_token: int = 0
    ) -> None:
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 0
        ):
            raise ValueError("fencing_token must be a non-negative integer")
        await self.client.insert(
            self.cursors_table,
            [(source_id, cursor or "", fencing_token, datetime.now(timezone.utc))],
            column_names=["source_id", "cursor", "fencing_token", "updated_at"],
        )

    async def ingest(
        self,
        provider: HistoricalProvider,
        *,
        cursor: str | None = None,
        fencing_token: int = 0,
    ) -> str | None:
        current_cursor = (
            cursor if cursor is not None else await self.load_cursor(provider.source_id)
        )
        page = await provider.fetch_page(cursor=current_cursor)
        if page.source_id != provider.source_id:
            raise ValueError("provider/page source identity mismatch")
        if any(item.source_id != page.source_id for item in page.provenance):
            raise ValueError("provenance source identity mismatch")
        # Persist provenance and revisions before checkpointing. A crash before
        # save_cursor safely replays the page; stable revision IDs deduplicate it.
        await self.append_provenance(page.provenance)
        await self.revisions.append_event_revisions(list(page.event_revisions))
        await self.revisions.append_market_bar_revisions(
            list(page.market_bar_revisions)
        )
        await self.save_cursor(
            page.source_id, page.next_cursor, fencing_token=fencing_token
        )
        return page.next_cursor


__all__ = [
    "HistoricalBarProvider",
    "HistoricalIngestionStore",
    "HistoricalProvider",
    "HistoricalProviderPage",
    "NormalizedProviderHistoryAdapter",
    "SourceProvenance",
]
