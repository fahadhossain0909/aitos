"""Append-only ClickHouse storage for capital events and entity evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .contracts import CapitalEvent, EntityEvidence, EntityLabel


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()


class ClickHouseCapitalStore:
    def __init__(self, client: Any, database: str = "aitos"):
        if not database or not database.replace("_", "").isalnum():
            raise ValueError("invalid database identifier")
        self.client = client
        self.database = database

    @property
    def table(self):
        return f"{self.database}.capital_flow_events"

    @property
    def evidence_table(self):
        return f"{self.database}.capital_entity_evidence"

    @property
    def labels_table(self):
        return f"{self.database}.capital_entity_labels"

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
        await self.client.command(
            f"""CREATE TABLE IF NOT EXISTS {self.evidence_table} (
            evidence_id String, subject_id String, object_id String,
            relation LowCardinality(String), evidence_type LowCardinality(String),
            evidence_ref String, observed_at DateTime64(3,'UTC'),
            confidence Float64, status LowCardinality(String),
            ingested_at DateTime64(6,'UTC')
        ) ENGINE=ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(observed_at) ORDER BY evidence_id"""
        )
        await self.client.command(
            f"""CREATE TABLE IF NOT EXISTS {self.labels_table} (
            label_id String, entity_id String, label String,
            category LowCardinality(String), source String, verified UInt8,
            valid_from DateTime64(3,'UTC'), valid_until Nullable(DateTime64(3,'UTC')),
            ingested_at DateTime64(6,'UTC')
        ) ENGINE=ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(valid_from) ORDER BY label_id"""
        )

    async def append(self, events: list[CapitalEvent]) -> int:
        if not events:
            return 0
        now = datetime.now(timezone.utc)
        rows = [
            (
                e.source_id, e.deduplication_key, e.event_id, e.event_time,
                e.observed_time, e.market, e.venue, e.instrument, e.event_type,
                e.actor_id or "", e.counterparty_id or "", e.transaction_id or "",
                e.block_number, e.block_hash or "", e.finality, e.quantity, e.unit,
                e.usd_value, e.confidence, e.to_json(), now,
            )
            for e in events
        ]
        columns = [
            "source_id", "deduplication_key", "event_id", "event_time",
            "observed_time", "market", "venue", "instrument", "event_type",
            "actor_id", "counterparty_id", "transaction_id", "block_number",
            "block_hash", "finality", "quantity", "unit", "usd_value",
            "confidence", "payload_json", "ingested_at",
        ]
        await self.client.insert(self.table, rows, column_names=columns)
        return len(rows)

    async def append_entity_evidence(self, evidence: list[EntityEvidence]) -> int:
        """Persist immutable relationship evidence; never promote its status."""
        if not evidence:
            return 0
        now = datetime.now(timezone.utc)
        rows = []
        for item in evidence:
            payload = {
                "subject_id": item.subject_id,
                "object_id": item.object_id,
                "relation": item.relation,
                "evidence_type": item.evidence_type,
                "evidence_ref": item.evidence_ref,
                "observed_at": item.observed_at.isoformat(),
                "confidence": item.confidence,
                "status": item.status,
            }
            rows.append(
                (
                    _stable_id("evi_", payload), item.subject_id, item.object_id,
                    item.relation, item.evidence_type, item.evidence_ref,
                    item.observed_at, item.confidence, item.status, now,
                )
            )
        await self.client.insert(
            self.evidence_table, rows,
            column_names=[
                "evidence_id", "subject_id", "object_id", "relation",
                "evidence_type", "evidence_ref", "observed_at", "confidence",
                "status", "ingested_at",
            ],
        )
        return len(rows)

    async def append_entity_labels(self, labels: list[EntityLabel]) -> int:
        """Persist temporal labels with their source and verification state."""
        if not labels:
            return 0
        now = datetime.now(timezone.utc)
        rows = []
        for item in labels:
            payload = {
                "entity_id": item.entity_id, "label": item.label,
                "category": item.category, "source": item.source,
                "verified": item.verified, "valid_from": item.valid_from.isoformat(),
                "valid_until": item.valid_until.isoformat() if item.valid_until else None,
            }
            rows.append(
                (
                    _stable_id("lbl_", payload), item.entity_id, item.label,
                    item.category, item.source, int(item.verified),
                    item.valid_from, item.valid_until, now,
                )
            )
        await self.client.insert(
            self.labels_table, rows,
            column_names=[
                "label_id", "entity_id", "label", "category", "source",
                "verified", "valid_from", "valid_until", "ingested_at",
            ],
        )
        return len(rows)

    async def reconcile_evm_reorg(self, instruction: dict[str, Any]) -> int:
        """Append reverted versions for stored logs whose block hash is no longer canonical."""
        if not hasattr(self.client, "query"):
            raise RuntimeError("ClickHouse query support is required for EVM reorg recovery")
        source_id = str(instruction["source_id"])
        start = int(instruction["from_block"])
        end = int(instruction["to_block"])
        canonical = {
            int(number): str(block_hash).lower()
            for number, block_hash in instruction["canonical_hashes"].items()
        }
        if not canonical or start < 0 or end < start:
            raise ValueError("invalid EVM reorg reconciliation range")
        result = await self.client.query(
            f"""SELECT payload_json, block_number, block_hash
            FROM {self.table}
            WHERE source_id = %(source_id)s
              AND block_number >= %(from_block)s
              AND block_number <= %(to_block)s""",
            parameters={"source_id": source_id, "from_block": start, "to_block": end},
        )
        tombstones = []
        detected_at = instruction.get("detected_at") or datetime.now(timezone.utc).isoformat()
        for payload_json, block_number, block_hash in result.result_rows:
            number = int(block_number)
            canonical_hash = canonical.get(number)
            if canonical_hash is None or str(block_hash).lower() == canonical_hash:
                continue
            payload = json.loads(payload_json)
            payload["event_time"] = datetime.fromisoformat(payload["event_time"])
            payload["observed_time"] = datetime.fromisoformat(payload["observed_time"])
            original = CapitalEvent(**payload)
            tombstones.append(
                replace(
                    original,
                    finality="reverted",
                    metadata={
                        **dict(original.metadata),
                        "reorg_status": "orphaned_log",
                        "reorg_detected_at": detected_at,
                        "canonical_block_hash": canonical_hash,
                    },
                )
            )
        return await self.append(tombstones)
