"""Durable ClickHouse storage for point-in-time event and market revisions.

Rows are append-only and keyed by source revision identity. Replaying the same
revision is idempotent after ReplacingMergeTree merges/FINAL reads. The store
keeps ingestion time separately from provider availability time so historical
reconstruction can distinguish when data was actually observed by AITOS.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from aitos.world_intelligence.point_in_time import (
    EventRevision,
    MarketBarRevision,
)


class AsyncClickHouseRevisionClient(Protocol):
    async def command(self, query: str) -> Any: ...

    async def insert(
        self, table: str, data: list[tuple], column_names: list[str]
    ) -> Any: ...

    async def query(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> Any: ...


class ClickHousePointInTimeStore:
    """Persist immutable point-in-time revisions and rehydrate bounded datasets."""

    def __init__(self, client: AsyncClickHouseRevisionClient, database: str = "aitos"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("database must be a simple ClickHouse identifier")
        self.client = client
        self.database = database

    @property
    def event_revisions_table(self) -> str:
        return f"{self.database}.world_intelligence_event_revisions"

    @property
    def market_bar_revisions_table(self) -> str:
        return f"{self.database}.world_intelligence_market_bar_revisions"

    async def ensure_schema(self) -> None:
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.event_revisions_table}
            (
                event_id String,
                revision_id String,
                asset LowCardinality(String),
                published_at DateTime64(3, 'UTC'),
                known_at DateTime64(3, 'UTC'),
                ingested_at DateTime64(6, 'UTC'),
                payload_json String
            )
            ENGINE = ReplacingMergeTree(ingested_at)
            PARTITION BY toYYYYMM(known_at)
            ORDER BY (asset, event_id, revision_id)
            """
        )
        await self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.market_bar_revisions_table}
            (
                asset LowCardinality(String),
                open_time DateTime64(3, 'UTC'),
                revision_id String,
                close_time DateTime64(3, 'UTC'),
                available_at DateTime64(3, 'UTC'),
                ingested_at DateTime64(6, 'UTC'),
                open_price Float64,
                close_price Float64,
                payload_json String
            )
            ENGINE = ReplacingMergeTree(ingested_at)
            PARTITION BY toYYYYMM(open_time)
            ORDER BY (asset, open_time, revision_id)
            """
        )

    async def append_event_revisions(self, revisions: list[EventRevision]) -> int:
        keys = [(item.event_id, item.revision_id) for item in revisions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate event revision identity in batch")
        now = datetime.now(timezone.utc)
        rows = [
            (
                item.event_id,
                item.revision_id,
                item.asset,
                item.published_at,
                item.known_at,
                now,
                json.dumps(
                    {
                        "event_id": item.event_id,
                        "revision_id": item.revision_id,
                        "asset": item.asset,
                        "event_type": item.event_type,
                        "published_at": item.published_at.isoformat(),
                        "known_at": item.known_at.isoformat(),
                        "sentiment": item.sentiment,
                        "severity": item.severity,
                        "novelty": item.novelty,
                        "confidence": item.confidence,
                        "source_reliability": item.source_reliability,
                        "regime": item.regime,
                        "beta": item.beta,
                    },
                    separators=(",", ":"),
                ),
            )
            for item in revisions
        ]
        if rows:
            await self.client.insert(
                self.event_revisions_table,
                rows,
                column_names=[
                    "event_id", "revision_id", "asset", "published_at",
                    "known_at", "ingested_at", "payload_json",
                ],
            )
        return len(rows)

    async def append_market_bar_revisions(
        self, revisions: list[MarketBarRevision]
    ) -> int:
        keys = [(item.asset, item.open_time, item.revision_id) for item in revisions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate market bar revision identity in batch")
        now = datetime.now(timezone.utc)
        rows = [
            (
                item.asset,
                item.open_time,
                item.revision_id,
                item.close_time,
                item.available_at,
                now,
                item.open_price,
                item.close_price,
                json.dumps(
                    {
                        "asset": item.asset,
                        "revision_id": item.revision_id,
                        "open_time": item.open_time.isoformat(),
                        "close_time": item.close_time.isoformat(),
                        "available_at": item.available_at.isoformat(),
                        "open_price": item.open_price,
                        "close_price": item.close_price,
                    },
                    separators=(",", ":"),
                ),
            )
            for item in revisions
        ]
        if rows:
            await self.client.insert(
                self.market_bar_revisions_table,
                rows,
                column_names=[
                    "asset",
                    "open_time",
                    "revision_id",
                    "close_time",
                    "available_at",
                    "ingested_at",
                    "open_price",
                    "close_price",
                    "payload_json",
                ],
            )
        return len(rows)

    async def load_revisions(
        self, *, as_of: datetime, limit: int = 100_000
    ) -> tuple[tuple[EventRevision, ...], tuple[MarketBarRevision, ...]]:
        """Load only revisions ingested by as_of; keep provider availability intact."""
        if not isinstance(as_of, datetime) or as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not 1 <= limit <= 1_000_000:
            raise ValueError("limit must be between 1 and 1000000")
        cutoff = as_of.astimezone(timezone.utc)
        stamp = cutoff.replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds")
        events = await self.client.query(
            f"SELECT payload_json FROM {self.event_revisions_table} FINAL "
            "WHERE ingested_at <= toDateTime64({as_of:String}, 3, 'UTC') "
            "ORDER BY known_at, event_id LIMIT {limit:UInt32}",
            parameters={"as_of": stamp, "limit": limit},
        )
        bars = await self.client.query(
            f"SELECT payload_json FROM {self.market_bar_revisions_table} FINAL "
            "WHERE ingested_at <= toDateTime64({as_of:String}, 3, 'UTC') "
            "ORDER BY asset, open_time, available_at LIMIT {limit:UInt32}",
            parameters={"as_of": stamp, "limit": limit},
        )
        event_rows_list = []
        for row in events.result_rows:
            payload = json.loads(row[0])
            for field in ("published_at", "known_at"):
                payload[field] = datetime.fromisoformat(payload[field])
            event_rows_list.append(EventRevision(**payload))
        bar_rows_list = []
        for row in bars.result_rows:
            payload = json.loads(row[0])
            for field in ("open_time", "close_time", "available_at"):
                payload[field] = datetime.fromisoformat(payload[field])
            bar_rows_list.append(MarketBarRevision(**payload))
        event_rows = tuple(event_rows_list)
        bar_rows = tuple(bar_rows_list)
        return event_rows, bar_rows


__all__ = ["ClickHousePointInTimeStore"]
