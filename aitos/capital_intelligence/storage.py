"""Append-only ClickHouse storage for normalized capital events."""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from .contracts import CapitalEvent


class ClickHouseCapitalStore:
    def __init__(self, client: Any, database: str = "aitos"):
        if not database or not database.replace("_", "").isalnum():
            raise ValueError("invalid database identifier")
        self.client = client
        self.database = database

    @property
    def table(self):
        return f"{self.database}.capital_flow_events"

    async def ensure_schema(self):
        await self.client.command(
            f"""CREATE TABLE IF NOT EXISTS {self.table} (
            source_id LowCardinality(String), deduplication_key String, event_id String,
            event_time DateTime64(3,'UTC'), observed_time DateTime64(3,'UTC'),
            market LowCardinality(String), venue LowCardinality(String), instrument String,
            event_type LowCardinality(String), actor_id String, counterparty_id String,
            transaction_id String, block_number Nullable(UInt64), block_hash String,
            finality LowCardinality(String), quantity Float64, unit String,
            usd_value Nullable(Float64), confidence Float64, payload_json String,
            ingested_at DateTime64(6,'UTC')
        ) ENGINE=ReplacingMergeTree(ingested_at) PARTITION BY toYYYYMM(event_time)
        ORDER BY (source_id,deduplication_key)"""
        )

    async def append(self, events: list[CapitalEvent]) -> int:
        if not events:
            return 0
        now = datetime.now(timezone.utc)
        rows = [
            (
                e.source_id,
                e.deduplication_key,
                e.event_id,
                e.event_time,
                e.observed_time,
                e.market,
                e.venue,
                e.instrument,
                e.event_type,
                e.actor_id or "",
                e.counterparty_id or "",
                e.transaction_id or "",
                e.block_number,
                e.block_hash or "",
                e.finality,
                e.quantity,
                e.unit,
                e.usd_value,
                e.confidence,
                e.to_json(),
                now,
            )
            for e in events
        ]
        columns = [
            "source_id",
            "deduplication_key",
            "event_id",
            "event_time",
            "observed_time",
            "market",
            "venue",
            "instrument",
            "event_type",
            "actor_id",
            "counterparty_id",
            "transaction_id",
            "block_number",
            "block_hash",
            "finality",
            "quantity",
            "unit",
            "usd_value",
            "confidence",
            "payload_json",
            "ingested_at",
        ]
        await self.client.insert(self.table, rows, column_names=columns)
        return len(rows)
