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

    CAPABILITIES = frozenset(
        {
            "trade_snapshots",
            "journal_entries",
            "decision_journal",
            "outcome_attribution",
        }
    )

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
            from aitos.storage.postgres_migrations import apply_postgres_migrations

            await apply_postgres_migrations(self._pool)
        except Exception:
            await self._pool.close()
            self._pool = None
            raise
        self._initialized = True

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Any):
        return None

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
        identity = self._trade_snapshot_identity(payload)
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
                SELECT payload::text AS payload FROM aitos_operational_records
                WHERE record_type = 'JOURNAL_ENTRY' AND entity_id = $1
                ORDER BY recorded_at, record_id
                """,
                trade_id,
            )
        return [self._decode_payload(row["payload"]) for row in rows]

    async def save_decision(self, decision_id: str, snapshot: dict[str, Any]) -> None:
        await self._append("DECISION", decision_id, f"decision:{decision_id}", snapshot)

    async def link_trade(self, decision_id: str, trade: dict[str, Any]) -> None:
        trade_id = str(trade.get("trade_id") or "")
        if not trade_id:
            raise ValueError("trade_id is required to link a decision")
        await self._append(
            "TRADE_LINK", decision_id, f"decision:{decision_id}:trade:{trade_id}", trade
        )

    async def attribute_outcome(self, decision_id: str, trade: dict[str, Any]) -> None:
        trade_id = str(trade.get("trade_id") or trade.get("candidate_id") or "unlinked")
        payload = dict(trade)
        pnl = payload.get("pnl")
        risk = payload.get("risk_amount_usd")
        payload["r_multiple"] = (
            float(pnl) / float(risk)
            if pnl is not None and risk not in (None, 0, 0.0)
            else None
        )
        payload["holding_seconds"] = self._holding_seconds(
            payload.get("entry_time"), payload.get("exit_time")
        )
        await self._append(
            "OUTCOME",
            decision_id,
            f"decision:{decision_id}:outcome:{trade_id}",
            payload,
        )

    async def save_management_decision(
        self, decision_id: str, trade_id: str, snapshot: dict[str, Any]
    ) -> None:
        payload = {**snapshot, "trade_id": trade_id}
        await self._append(
            "MANAGEMENT_DECISION", decision_id, f"management:{decision_id}", payload
        )

    async def get_records(self, decision_id: str) -> list[dict[str, Any]]:
        return await self._get_records(
            "SELECT record_type, entity_id, recorded_at, payload::text AS payload "
            "FROM aitos_operational_records WHERE entity_id = $1 "
            "ORDER BY recorded_at, record_id",
            decision_id,
        )

    async def get_trade_records(self, trade_id: str) -> list[dict[str, Any]]:
        return await self._get_records(
            "SELECT record_type, entity_id, recorded_at, payload::text AS payload "
            "FROM aitos_operational_records WHERE entity_id = $1 "
            "OR payload->>'trade_id' = $1 ORDER BY recorded_at, record_id",
            trade_id,
        )

    async def _get_records(self, query: str, key: str) -> list[dict[str, Any]]:
        self._require_initialized()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, key)
        records = []
        for row in rows:
            payload = self._decode_payload(row["payload"])
            records.append(
                {
                    "record_type": row["record_type"],
                    "decision_id": row["entity_id"],
                    "recorded_at": row["recorded_at"],
                    "payload": json.dumps(payload, default=str),
                    **payload,
                }
            )
        return records

    @staticmethod
    def _holding_seconds(entry_time: Any, exit_time: Any) -> float | None:
        if not entry_time or not exit_time:
            return None
        try:
            start = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return max(0.0, (end - start).total_seconds())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {"value": decoded}
        return dict(value)

    async def _append(
        self, record_type: str, entity_id: str, key: str, payload: dict[str, Any]
    ) -> None:
        self._require_initialized()
        recorded_at = datetime.now(timezone.utc)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
            allow_nan=False,
        )
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
    def _trade_snapshot_identity(payload: dict[str, Any]) -> dict[str, Any]:
        """Exclude the generated write timestamp while retaining all trade state."""
        return {key: value for key, value in payload.items() if key != "recorded_at"}

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
