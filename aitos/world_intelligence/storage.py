"""Append-only ClickHouse event storage with replay-safe cursor checkpoints.

The write protocol is deliberately at-least-once: persist the event page first,
then persist its source cursor. A crash between those operations replays the page;
the ReplacingMergeTree key makes that replay idempotent after merges/FINAL reads.
Redis per-source leases coordinate writers; monotonic fencing tokens protect cursor checkpoints even if an expired worker resumes. ClickHouse still does not provide a transaction spanning event persistence and Redis publication, so delivery remains at-least-once.
"""

from __future__ import annotations

import json
import math
import re
import uuid
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
        self,
        client: AsyncClickHouseClient,
        database: str = "aitos",
        *,
        max_pending_dead_letters_per_source: int = 1000,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("database must be a simple ClickHouse identifier")
        if not 1 <= max_pending_dead_letters_per_source <= 10000:
            raise ValueError("max pending dead letters must be between 1 and 10000")
        self.client = client
        self.database = database
        self.max_pending_dead_letters_per_source = max_pending_dead_letters_per_source

    @property
    def events_table(self) -> str:
        return f"{self.database}.world_intelligence_events"

    @property
    def cursors_table(self) -> str:
        return f"{self.database}.world_intelligence_cursors"

    @property
    def dead_letters_table(self) -> str:
        return f"{self.database}.world_intelligence_dead_letters"

    @property
    def source_runs_table(self) -> str:
        return f"{self.database}.world_intelligence_source_runs"

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
                cursor String,
                fencing_token UInt64 DEFAULT 0,
                updated_at DateTime64(6, 'UTC')
            )
            ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY source_id
            """
        )
        # Upgrade existing installations without requiring a table rebuild.
        await self.client.command(
            f"ALTER TABLE {self.cursors_table} ADD COLUMN IF NOT EXISTS "
            "fencing_token UInt64 DEFAULT 0"
        )
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.dead_letters_table}
            (
                failure_id String,
                source_id LowCardinality(String),
                cursor_before String,
                cursor_after String,
                payload_json String,
                error_type LowCardinality(String),
                error_message String,
                attempts UInt32,
                status LowCardinality(String),
                created_at DateTime64(6, 'UTC'),
                updated_at DateTime64(6, 'UTC')
            )
            ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (source_id, failure_id)
            TTL toDateTime(created_at) + INTERVAL 90 DAY DELETE WHERE status != 'pending'
            """
        )
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.source_runs_table}
            (
                run_id String,
                source_id LowCardinality(String),
                started_at DateTime64(3, 'UTC'),
                finished_at DateTime64(3, 'UTC'),
                status LowCardinality(String),
                event_count UInt32,
                cursor_before String,
                cursor_after String,
                error_type LowCardinality(String),
                fencing_token UInt64,
                latency_ms Float64
            )
            ENGINE = MergeTree
            PARTITION BY toYYYYMM(started_at)
            ORDER BY (source_id, started_at, run_id)
            TTL toDateTime(started_at) + INTERVAL 180 DAY DELETE
            """
        )

    async def load_cursor(self, source_id: str) -> str | None:
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        result = await self.client.query(
            f"SELECT argMax(cursor, (fencing_token, updated_at)) FROM {self.cursors_table} "  # nosec B608 - database is identifier-validated; table name is fixed
            "WHERE source_id = {source_id:String}",
            parameters={"source_id": source_id},
        )
        if not result.result_rows:
            return None
        return result.result_rows[0][0] or None

    async def append_events(
        self, source_id: str, events: list[IntelligenceEvent]
    ) -> int:
        """Append a normalized page without moving its source checkpoint."""
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
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
        return len(rows)

    async def save_cursor(
        self, source_id: str, cursor: str | None, *, fencing_token: int = 0
    ) -> None:
        """Advance a cursor; older lease generations cannot supersede newer ones."""
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string or None")
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

    async def load_recent_events(
        self,
        *,
        as_of: datetime,
        lookback_hours: float = 72.0,
        limit: int = 5000,
    ) -> list[IntelligenceEvent]:
        """Load bounded, point-in-time evidence for shadow snapshot generation."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not 0 < lookback_hours <= 24 * 90 or not 1 <= limit <= 20_000:
            raise ValueError("invalid event query lookback or limit")
        cutoff = as_of.astimezone(timezone.utc)
        from datetime import timedelta

        lower_bound = cutoff - timedelta(hours=lookback_hours)
        result = await self.client.query(
            f"SELECT payload_json FROM {self.events_table} FINAL "  # nosec B608 - validated fixed table identifier
            "WHERE published_at <= toDateTime64({as_of:String}, 3, 'UTC') "
            "AND first_seen_at <= toDateTime64({as_of:String}, 3, 'UTC') "
            "AND first_seen_at >= toDateTime64({lower_bound:String}, 3, 'UTC') "
            "ORDER BY first_seen_at DESC LIMIT {limit:UInt32}",
            parameters={
                "as_of": cutoff.replace(tzinfo=None).isoformat(
                    sep=" ", timespec="milliseconds"
                ),
                "lower_bound": lower_bound.replace(tzinfo=None).isoformat(
                    sep=" ", timespec="milliseconds"
                ),
                "limit": limit,
            },
        )
        events: list[IntelligenceEvent] = []
        for row in result.result_rows:
            try:
                payload = json.loads(row[0])
                event = IntelligenceEvent.from_dict(payload)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if event.published_at <= cutoff and event.first_seen_at <= cutoff:
                events.append(event)
        return events

    async def persist_page(
        self,
        source_id: str,
        events: list[IntelligenceEvent],
        next_cursor: str | None,
        *,
        fencing_token: int = 0,
    ) -> int:
        """Persist a page before advancing its checkpoint.

        This convenience method provides at-least-once persistence. Callers
        that also publish to Redis should use append_events(), publish, then
        save_cursor() so a failed publish cannot lose the page.
        """
        count = await self.append_events(source_id, events)
        await self.save_cursor(source_id, next_cursor, fencing_token=fencing_token)
        return count

    async def record_source_run(
        self,
        *,
        source_id: str,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        event_count: int,
        cursor_before: str | None = None,
        cursor_after: str | None = None,
        error_type: str = "",
        fencing_token: int = 0,
        latency_ms: float = 0.0,
    ) -> str:
        """Persist bounded per-source operational evidence for 180 days."""
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        if status not in {"success", "failed", "lease_lost", "lease_unavailable"}:
            raise ValueError("unsupported source run status")
        if (
            started_at.tzinfo is None
            or finished_at.tzinfo is None
            or finished_at < started_at
        ):
            raise ValueError("source run timestamps must be ordered and timezone-aware")
        if (
            isinstance(event_count, bool)
            or not isinstance(event_count, int)
            or event_count < 0
        ):
            raise ValueError("event_count must be a non-negative integer")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 0
        ):
            raise ValueError("fencing_token must be a non-negative integer")
        latency = float(latency_ms)
        if not math.isfinite(latency) or latency < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        run_id = uuid.uuid4().hex
        await self.client.insert(
            self.source_runs_table,
            [
                (
                    run_id,
                    source_id,
                    started_at.astimezone(timezone.utc),
                    finished_at.astimezone(timezone.utc),
                    status,
                    event_count,
                    cursor_before or "",
                    cursor_after or "",
                    error_type[:80],
                    fencing_token,
                    latency,
                )
            ],
            column_names=[
                "run_id",
                "source_id",
                "started_at",
                "finished_at",
                "status",
                "event_count",
                "cursor_before",
                "cursor_after",
                "error_type",
                "fencing_token",
                "latency_ms",
            ],
        )
        return run_id

    async def load_source_run_summary(
        self,
        *,
        as_of: datetime,
        lookback_hours: float = 24.0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return bounded, point-in-time per-source ingestion health aggregates."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not 0 < lookback_hours <= 24 * 90 or not 1 <= limit <= 1000:
            raise ValueError("invalid source-run lookback or limit")
        from datetime import timedelta

        cutoff = as_of.astimezone(timezone.utc)
        lower_bound = cutoff - timedelta(hours=lookback_hours)
        result = await self.client.query(
            f"SELECT source_id, count(), countIf(status = 'success'), "
            f"countIf(status = 'failed'), countIf(status = 'lease_lost'), "
            f"countIf(status = 'lease_unavailable'), sum(event_count), "
            f"avg(latency_ms), argMax(status, finished_at), max(finished_at) "
            f"FROM {self.source_runs_table} "  # nosec B608 - fixed table property and validated database
            "WHERE finished_at <= toDateTime64({as_of:String}, 3, 'UTC') "
            "AND started_at >= toDateTime64({lower_bound:String}, 3, 'UTC') "
            "GROUP BY source_id ORDER BY source_id LIMIT {limit:UInt32}",
            parameters={
                # ClickHouse toDateTime64(String) does not accept ISO-8601
                # UTC offsets. Send explicit UTC wall-clock values instead.
                "as_of": cutoff.replace(tzinfo=None).isoformat(
                    sep=" ", timespec="milliseconds"
                ),
                "lower_bound": lower_bound.replace(tzinfo=None).isoformat(
                    sep=" ", timespec="milliseconds"
                ),
                "limit": limit,
            },
        )
        return [
            {
                "source_id": row[0],
                "runs": int(row[1]),
                "successes": int(row[2]),
                "failures": int(row[3]),
                "lease_lost": int(row[4]),
                "lease_contention": int(row[5]),
                "events": int(row[6] or 0),
                "mean_latency_ms": round(float(row[7] or 0.0), 3),
                "latest_status": row[8],
                "last_finished_at": (
                    row[9].isoformat() if hasattr(row[9], "isoformat") else str(row[9])
                ),
            }
            for row in result.result_rows
        ]

    async def record_dead_letter(
        self,
        *,
        source_id: str,
        cursor_before: str | None,
        cursor_after: str | None,
        events: list[IntelligenceEvent],
        error: Exception,
    ) -> str:
        """Persist a failed page for operator inspection and explicit replay."""
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        pending = await self.client.query(
            f"SELECT count() FROM {self.dead_letters_table} FINAL "  # nosec B608 - fixed table property; database identifier is validated
            "WHERE source_id = {source_id:String} AND status = 'pending'",
            parameters={"source_id": source_id},
        )
        pending_count = int(pending.result_rows[0][0]) if pending.result_rows else 0
        if pending_count >= self.max_pending_dead_letters_per_source:
            raise RuntimeError(
                "WIATS dead-letter queue is at its per-source safety limit"
            )
        failure_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        payload = json.dumps(
            [event.to_dict() for event in events], separators=(",", ":")
        )
        await self.client.insert(
            self.dead_letters_table,
            [
                (
                    failure_id,
                    source_id,
                    cursor_before or "",
                    cursor_after or "",
                    payload,
                    type(error).__name__,
                    type(error).__name__,
                    1,
                    "pending",
                    now,
                    now,
                )
            ],
            column_names=[
                "failure_id",
                "source_id",
                "cursor_before",
                "cursor_after",
                "payload_json",
                "error_type",
                "error_message",
                "attempts",
                "status",
                "created_at",
                "updated_at",
            ],
        )
        return failure_id

    async def load_pending_dead_letters(
        self, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("dead-letter limit must be between 1 and 1000")
        result = await self.client.query(
            f"SELECT failure_id, source_id, cursor_before, cursor_after, "
            f"payload_json, attempts FROM {self.dead_letters_table} FINAL "  # nosec B608 - fixed table property; database identifier is validated
            "WHERE status = 'pending' ORDER BY created_at ASC LIMIT {limit:UInt32}",
            parameters={"limit": limit},
        )
        rows: list[dict[str, Any]] = []
        for row in result.result_rows:
            try:
                payload = json.loads(row[4])
                events = [IntelligenceEvent.from_dict(item) for item in payload]
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "failure_id": row[0],
                    "source_id": row[1],
                    "cursor_before": row[2] or None,
                    "cursor_after": row[3] or None,
                    "events": events,
                    "attempts": int(row[5]),
                }
            )
        return rows

    async def update_dead_letter(
        self, failure_id: str, *, status: str, attempts: int
    ) -> None:
        if not failure_id or status not in {"pending", "replayed", "discarded"}:
            raise ValueError("invalid dead-letter update")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        result = await self.client.query(
            f"SELECT failure_id, source_id, cursor_before, cursor_after, "
            f"payload_json, error_type, error_message, created_at "
            f"FROM {self.dead_letters_table} FINAL WHERE failure_id = {{failure_id:String}} "  # nosec B608 - fixed table property; database identifier is validated
            "LIMIT 1",
            parameters={"failure_id": failure_id},
        )
        if not result.result_rows:
            raise KeyError(f"dead-letter {failure_id!r} not found")
        row = result.result_rows[0]
        await self.client.insert(
            self.dead_letters_table,
            [
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    attempts,
                    status,
                    row[7],
                    datetime.now(timezone.utc),
                )
            ],
            column_names=[
                "failure_id",
                "source_id",
                "cursor_before",
                "cursor_after",
                "payload_json",
                "error_type",
                "error_message",
                "attempts",
                "status",
                "created_at",
                "updated_at",
            ],
        )
