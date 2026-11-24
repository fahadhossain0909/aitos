"""PostgreSQL-backed canonical OMS journal.

This store is the multi-process authority for order/trade aggregate revisions.

SQLite execution-intent records and ClickHouse tables are recovery evidence and
projections; neither may supersede a committed PostgreSQL OMS event.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}\Z")


class OMSConflict(RuntimeError):
    """A stale writer, duplicate event, or aggregate revision conflict."""


class OMSIntegrityError(RuntimeError):
    """Persisted OMS history does not match its cryptographic hash chain."""


@dataclass(frozen=True)
class WriterFence:
    account_scope: str
    writer_id: str
    generation: int


@dataclass(frozen=True)
class OMSEvent:
    account_scope: str
    aggregate_id: str
    revision: int
    event_id: str
    writer_id: str
    generation: int
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"valid bounded {label} is required")
    return value


def _canonical_payload(payload: dict[str, Any]) -> str:
    if type(payload) is not dict or not payload:
        raise ValueError("non-empty OMS event object is required")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("OMS event payload exceeds 64 KiB")
    return encoded


def _event_hash(
    scope: str,
    aggregate: str,
    revision: int,
    event_id: str,
    writer_id: str,
    generation: int,
    previous_hash: str,
    payload_json: str,
) -> str:
    material = "\\0".join(
        (
            scope,
            aggregate,
            str(revision),
            event_id,
            writer_id,
            str(generation),
            previous_hash,
            payload_json,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PostgresCanonicalOMS:
    """Transactional scope-keyed journal with CAS, fencing and outbox.

    The supplied pool must target the primary PostgreSQL writer. Read replicas
    are intentionally unsupported because fencing and CAS require linearizable
    writes. All mutations are transaction-scoped and use row locks.
    """

    def __init__(self, pool):
        if pool is None or not callable(getattr(pool, "acquire", None)):
            raise ValueError("an initialized asyncpg primary pool is required")
        self._pool = pool

    async def acquire_writer(
        self,
        account_scope: str,
        writer_id: str,
        *,
        expected_generation: int,
    ) -> WriterFence:
        scope = _identifier(account_scope, "account_scope")
        writer = _identifier(writer_id, "writer_id")
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a non-negative integer")
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", scope)
            row = await conn.fetchrow(
                """SELECT writer_id,generation FROM aitos_oms_writer_fences
                   WHERE account_scope=$1 FOR UPDATE""",
                scope,
            )
            actual = int(row["generation"]) if row else 0
            if actual != expected_generation:
                raise OMSConflict(
                    f"writer generation CAS failed: expected {expected_generation}, actual {actual}"
                )
            generation = actual + 1
            await conn.execute(
                """INSERT INTO aitos_oms_writer_fences
                   (account_scope,writer_id,generation) VALUES($1,$2,$3)
                   ON CONFLICT(account_scope) DO UPDATE SET
                     writer_id=EXCLUDED.writer_id,generation=EXCLUDED.generation""",
                scope, writer, generation,
            )
        return WriterFence(scope, writer, generation)

    async def append(
        self,
        fence: WriterFence,
        *,
        aggregate_id: str,
        event_id: str,
        expected_revision: int,
        payload: dict[str, Any],
    ) -> OMSEvent:
        if type(fence) is not WriterFence:
            raise OMSConflict("a server-issued writer fence is required")
        scope = _identifier(fence.account_scope, "account_scope")
        aggregate = _identifier(aggregate_id, "aggregate_id")
        event = _identifier(event_id, "event_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        encoded = _canonical_payload(payload)
        async with self._pool.acquire() as conn, conn.transaction():
            owner = await conn.fetchrow(
                """SELECT writer_id,generation FROM aitos_oms_writer_fences
                   WHERE account_scope=$1 FOR UPDATE""", scope,
            )
            if owner is None or (
                owner["writer_id"], int(owner["generation"])
            ) != (fence.writer_id, fence.generation):
                raise OMSConflict("stale writer fencing token")
            duplicate = await conn.fetchrow(
                """SELECT aggregate_id,revision,payload::text AS payload_json,
                          writer_id,generation,previous_hash,event_hash
                   FROM aitos_oms_events
                   WHERE account_scope=$1 AND event_id=$2""", scope, event,
            )
            if duplicate is not None:
                if duplicate["aggregate_id"] != aggregate or json.loads(
                    duplicate["payload_json"]
                ) != json.loads(encoded):
                    raise OMSConflict("event id reused with different content")
                return OMSEvent(
                    scope,
                    aggregate,
                    int(duplicate["revision"]),
                    event,
                    duplicate["writer_id"],
                    int(duplicate["generation"]),
                    json.loads(duplicate["payload_json"]),
                    duplicate["previous_hash"],
                    duplicate["event_hash"],
                )
            await conn.execute(
                """INSERT INTO aitos_oms_aggregate_heads
                   (account_scope,aggregate_id,revision,event_hash)
                   VALUES($1,$2,0,$3) ON CONFLICT DO NOTHING""",
                scope, aggregate, "0" * 64,
            )
            head = await conn.fetchrow(
                """SELECT revision,event_hash FROM aitos_oms_aggregate_heads
                   WHERE account_scope=$1 AND aggregate_id=$2 FOR UPDATE""",
                scope,
                aggregate,
            )
            actual_revision = int(head["revision"])
            if actual_revision != expected_revision:
                raise OMSConflict(
                    f"aggregate revision CAS failed: expected {expected_revision}, actual {actual_revision}"
                )
            revision = actual_revision + 1
            previous_hash = head["event_hash"]
            digest = _event_hash(
                scope, aggregate, revision, event, fence.writer_id,
                fence.generation, previous_hash, encoded,
            )
            await conn.execute(
                """INSERT INTO aitos_oms_events
                   (account_scope,aggregate_id,revision,event_id,writer_id,
                    generation,payload,previous_hash,event_hash)
                   VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)""",
                scope,
                aggregate,
                revision,
                event,
                fence.writer_id,
                fence.generation,
                encoded,
                previous_hash,
                digest,
            )
            await conn.execute(
                """UPDATE aitos_oms_aggregate_heads
                   SET revision=$3,event_hash=$4
                   WHERE account_scope=$1 AND aggregate_id=$2""",
                scope, aggregate, revision, digest,
            )
            await conn.execute(
                """INSERT INTO aitos_oms_outbox
                   (account_scope,aggregate_id,revision) VALUES($1,$2,$3)""",
                scope, aggregate, revision,
            )
        return OMSEvent(
            scope,
            aggregate,
            revision,
            event,
            fence.writer_id,
            fence.generation,
            json.loads(encoded),
            previous_hash,
            digest,
        )

    async def read_events(
        self, account_scope: str, aggregate_id: str
    ) -> list[OMSEvent]:
        scope = _identifier(account_scope, "account_scope")
        aggregate = _identifier(aggregate_id, "aggregate_id")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT account_scope,aggregate_id,revision,event_id,writer_id,
                          generation,payload::text AS payload_json,previous_hash,event_hash
                   FROM aitos_oms_events
                   WHERE account_scope=$1 AND aggregate_id=$2 ORDER BY revision""",
                scope, aggregate,
            )
        result = []
        previous = "0" * 64
        expected_revision = 1
        for row in rows:
            payload_json = _canonical_payload(json.loads(row["payload_json"]))
            digest = _event_hash(
                scope,
                aggregate,
                expected_revision,
                row["event_id"],
                row["writer_id"],
                int(row["generation"]),
                previous,
                payload_json,
            )
            if (
                int(row["revision"]) != expected_revision
                or row["previous_hash"] != previous
                or row["event_hash"] != digest
            ):
                raise OMSIntegrityError("PostgreSQL OMS hash-chain verification failed")
            result.append(
                OMSEvent(
                    scope,
                    aggregate,
                    expected_revision,
                    row["event_id"],
                    row["writer_id"],
                    int(row["generation"]),
                    json.loads(payload_json),
                    previous,
                    digest,
                )
            )
            previous = digest
            expected_revision += 1
        return result

    async def claim_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """SELECT o.account_scope,o.aggregate_id,o.revision,
                          e.event_id,e.payload::text AS payload_json,e.event_hash
                   FROM aitos_oms_outbox o JOIN aitos_oms_events e
                     USING(account_scope,aggregate_id,revision)
                   WHERE o.projected_at IS NULL
                   ORDER BY o.created_at,o.account_scope,o.aggregate_id,o.revision
                   LIMIT $1 FOR UPDATE OF o SKIP LOCKED""",
                limit,
            )
            return [dict(row) for row in rows]

    async def mark_projected(
        self,
        account_scope: str,
        aggregate_id: str,
        revision: int,
        *,
        event_hash: str,
    ) -> None:
        scope = _identifier(account_scope, "account_scope")
        aggregate = _identifier(aggregate_id, "aggregate_id")
        if type(revision) is not int or revision < 1:
            raise ValueError("positive revision required")
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                """UPDATE aitos_oms_outbox SET projected_at=now()
                   WHERE account_scope=$1 AND aggregate_id=$2 AND revision=$3
                     AND EXISTS (SELECT 1 FROM aitos_oms_events e
                       WHERE e.account_scope=$1 AND e.aggregate_id=$2
                         AND e.revision=$3 AND e.event_hash=$4)
                     AND projected_at IS NULL""",
                scope,
                aggregate,
                revision,
                event_hash,
            )
            if result != "UPDATE 1":
                raise OMSConflict(
                    "projection acknowledgement missing, stale, or duplicated"
                )
