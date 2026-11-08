"""PostgreSQL fallback for operational journal records.

This adapter is intentionally not a replacement for ClickHouse market-data,
feature-store, or analytical queries. It provides transactional, idempotent
persistence for trade snapshots and journal entries only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    HealthStatus,
    ModuleStatus,
)
from aitos.journal.models import JournalEntry


class PostgresJournalRepository:
    """Transactional operational journal adapter backed by asyncpg."""

    CAPABILITIES = frozenset({"trade_snapshots", "journal_entries"})

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool = None
        self._initialized = False
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "postgres-journal-repository"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        if self._initialized:
            return
        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS aitos_operational_records (
                        record_id BIGSERIAL PRIMARY KEY,
                        record_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        recorded_at TIMESTAMPTZ NOT NULL,
                        payload JSONB NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_aitos_operational_records_entity
                    ON aitos_operational_records (record_type, entity_id, recorded_at)
                    """
                )
        except Exception:
            await self._pool.close()
            self._pool = None
            raise
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        import time

        started = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            status = ModuleStatus.HEALTHY
        except Exception:
            status = ModuleStatus.UNHEALTHY
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=(time.monotonic() - started) * 1000,
            last_event_time=self._last_event_time,
            details={"backend": "postgres", "scope": "operational_journal"},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._initialized = False

    async def save_trade_snapshot(self, trade_dict: dict[str, Any]) -> None:
        payload = dict(trade_dict)
        trade_id = str(payload.get("trade_id") or "")
        if not trade_id:
            raise ValueError("trade_id is required for durable trade snapshot")
        payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        identity_fields = (
            "trade_id",
            "snapshot_version",
            "updated_at",
            "state",
            "entry_time",
            "exit_time",
            "quantity",
            "exit_price",
            "pnl",
            "pnl_percent",
        )
        identity = {key: payload.get(key) for key in identity_fields}
        await self._append(
            "TRADE_SNAPSHOT",
            trade_id,
            self._idempotency_key("trade", identity),
            payload,
        )

    async def save_journal_entry(self, entry: JournalEntry) -> None:
        payload = {
            "entry_id": entry.entry_id,
            "trade_id": entry.trade_id,
            "entry_type": entry.entry_type.value,
            "market_context": entry.market_context,
            "confidence_score": entry.confidence_score,
            "order_flow_observations": entry.order_flow_observations,
            "liquidity_observations": entry.liquidity_observations,
            "amt_observations": entry.amt_observations,
            "lead_lag_observations": entry.lead_lag_observations,
            "mistakes": entry.mistakes,
            "lessons": entry.lessons,
            "improvements": entry.improvements,
        }
        await self._append(
            "JOURNAL_ENTRY",
            str(entry.trade_id or entry.entry_id),
            f"journal-entry:{entry.entry_id}",
            payload,
        )

    async def get_journal_entries_for_trade(
        self, trade_id: str
    ) -> list[dict[str, Any]]:
        self._require_initialized()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT payload FROM aitos_operational_records
                WHERE record_type = 'JOURNAL_ENTRY' AND entity_id = $1
                ORDER BY recorded_at, record_id
                """,
                trade_id,
            )
        return [dict(row["payload"]) for row in rows]

    async def _append(
        self, record_type: str, entity_id: str, key: str, payload: dict[str, Any]
    ) -> None:
        self._require_initialized()
        recorded_at = datetime.now(timezone.utc)
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO aitos_operational_records
                    (record_type, entity_id, idempotency_key, recorded_at, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                record_type,
                entity_id,
                key,
                recorded_at,
                encoded,
            )
        self._last_event_time = recorded_at.isoformat()

    @staticmethod
    def _idempotency_key(prefix: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"

    def _require_initialized(self) -> None:
        if not self._initialized or self._pool is None:
            raise RuntimeError("PostgresJournalRepository is not initialized")
