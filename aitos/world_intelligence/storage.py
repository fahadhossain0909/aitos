"""Append-only ClickHouse event storage with replay-safe cursor checkpoints.

The write protocol is deliberately at-least-once: persist the event page first,
then persist its source cursor. A crash between those operations replays the page;
the ReplacingMergeTree key makes that replay idempotent after merges/FINAL reads.
A single writer per source is required because ClickHouse does not provide a
transaction spanning the event and cursor tables.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from aitos.world_intelligence.contracts import IntelligenceEvent


class AsyncClickHouseClient(Protocol):
    """Subset of clickhouse-connect's async client used by this repository."""

    async def command(self, query: str) -> Any: ...

    async def insert(
        self, table: str, data: list[tuple], column_names: list[str]
    ) -> Any: ...

    async def query(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> Any: ...


class ClickHouseIntelligenceStore:
    """Persist normalized intelligence events and durable adapter cursors."""

    def __init__(
        self, client: AsyncClickHouseClient, database: str = "aitos"
    ) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("database must be a simple ClickHouse identifier")
        self.client = client
        self.database = database

    @property
    def events_table(self) -> str:
        return f"{self.database}.world_intelligence_events"

    @property
    def cursors_table(self) -> str:
        return f"{self.database}.world_intelligence_cursors"

    async def ensure_schema(self) -> None:
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.events_table}
            (
                source_id LowCardinality(String),
                deduplication_key String,
                event_id String,
                published_at DateTime64(3, 'UTC'),
                first_seen_at DateTime64(3, 'UTC'),
                ingested_at DateTime64(6, 'UTC'),
                event_type LowCardinality(String),
                verification LowCardinality(String),
                entities_json String,
                payload_json String
            )
            ENGINE = ReplacingMergeTree(ingested_at)
            PARTITION BY toYYYYMM(published_at)
            ORDER BY (source_id, deduplication_key)
            """
        )
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.cursors_table}
            (
                source_id LowCardinality(String),
                cursor Nullable(String),
                updated_at DateTime64(6, 'UTC')
            )
            ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY source_id
            """
        )

    async def load_cursor(self, source_id: str) -> str | None:
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        result = await self.client.query(
            f"SELECT argMax(cursor, updated_at) FROM {self.cursors_table} "
            "WHERE source_id = {source_id:String}",
            parameters={"source_id": source_id},
        )
        if not result.result_rows:
            return None
        return result.result_rows[0][0]

    async def persist_page(
        self,
        source_id: str,
        events: list[IntelligenceEvent],
        next_cursor: str | None,
    ) -> int:
        """Persist all events before advancing the cursor.

        On any event insert failure, the cursor is not advanced. Cursor-write
        failure is propagated; replay is safe and expected.
        """
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("cursor must be a string or None")
        for event in events:
            if event.source.source_id != source_id:
                raise ValueError("event source_id does not match page source")

        now = datetime.now(timezone.utc)
        rows = [
            (
                source_id,
                event.deduplication_key,
                event.event_id,
                event.published_at,
                event.first_seen_at,
                now,
                event.event_type,
                event.verification,
                json.dumps(event.entities, separators=(",", ":")),
                event.to_json(),
            )
            for event in events
        ]
        if rows:
            await self.client.insert(
                self.events_table,
                rows,
                column_names=[
                    "source_id",
                    "deduplication_key",
                    "event_id",
                    "published_at",
                    "first_seen_at",
                    "ingested_at",
                    "event_type",
                    "verification",
                    "entities_json",
                    "payload_json",
                ],
            )

        await self.client.insert(
            self.cursors_table,
            [(source_id, next_cursor, now)],
            column_names=["source_id", "cursor", "updated_at"],
        )
        return len(rows)
