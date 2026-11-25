"""PostgreSQL-backed canonical OMS journal.

This store is the multi-process authority for order/trade aggregate revisions.

SQLite execution-intent records and ClickHouse tables are recovery evidence and
projections; neither may supersede a committed PostgreSQL OMS event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", scope
            )
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
                scope,
                writer,
                generation,
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
        ledger_entries: list[dict[str, Any]] | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> OMSEvent:
        if type(fence) is not WriterFence:
            raise OMSConflict("a server-issued writer fence is required")
        scope = _identifier(fence.account_scope, "account_scope")
        aggregate = _identifier(aggregate_id, "aggregate_id")
        event = _identifier(event_id, "event_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        normalized_entries = self._validate_ledger_entries(
            [] if ledger_entries is None else ledger_entries
        )
        canonical_payload = dict(payload)
        if "financial_ledger" in canonical_payload:
            raise ValueError(
                "financial_ledger payload is reserved by the canonical OMS"
            )
        if any(
            value is not None
            for value in (causation_id, correlation_id, parent_event_id)
        ):
            if "causality" in canonical_payload:
                raise ValueError("causality metadata is reserved by the canonical OMS")
            canonical_payload["causality"] = {
                "causation_id": (
                    _identifier(causation_id, "causation_id")
                    if causation_id is not None
                    else None
                ),
                "correlation_id": (
                    _identifier(correlation_id, "correlation_id")
                    if correlation_id is not None
                    else None
                ),
                "parent_event_id": (
                    _identifier(parent_event_id, "parent_event_id")
                    if parent_event_id is not None
                    else None
                ),
            }
        if normalized_entries:
            canonical_payload["financial_ledger"] = [
                {
                    "account_code": item["account_code"],
                    "currency": item["currency"],
                    "debit": format(item["debit"], "f"),
                    "credit": format(item["credit"], "f"),
                    "reference": item["reference"],
                }
                for item in normalized_entries
            ]
        encoded = _canonical_payload(canonical_payload)
        async with self._pool.acquire() as conn, conn.transaction():
            owner = await conn.fetchrow(
                """SELECT writer_id,generation FROM aitos_oms_writer_fences
                   WHERE account_scope=$1 FOR UPDATE""",
                scope,
            )
            if owner is None or (owner["writer_id"], int(owner["generation"])) != (
                fence.writer_id,
                fence.generation,
            ):
                raise OMSConflict("stale writer fencing token")
            duplicate = await conn.fetchrow(
                """SELECT aggregate_id,revision,payload::text AS payload_json,
                          writer_id,generation,previous_hash,event_hash
                   FROM aitos_oms_events
                   WHERE account_scope=$1 AND event_id=$2""",
                scope,
                event,
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
                scope,
                aggregate,
                "0" * 64,
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
                scope,
                aggregate,
                revision,
                event,
                fence.writer_id,
                fence.generation,
                previous_hash,
                encoded,
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
                scope,
                aggregate,
                revision,
                digest,
            )
            await conn.execute(
                """INSERT INTO aitos_oms_outbox
                   (account_scope,aggregate_id,revision) VALUES($1,$2,$3)""",
                scope,
                aggregate,
                revision,
            )
            for line_no, entry in enumerate(normalized_entries):
                await conn.execute(
                    """INSERT INTO aitos_oms_financial_ledger
                       (account_scope,event_id,line_no,trade_id,account_code,
                        currency,debit,credit,reference)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    scope,
                    event,
                    line_no,
                    aggregate,
                    entry["account_code"],
                    entry["currency"],
                    entry["debit"],
                    entry["credit"],
                    entry["reference"],
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

    @staticmethod
    def _validate_ledger_entries(
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate balanced lines before opening the write transaction."""
        if not isinstance(entries, list):
            raise TypeError("ledger_entries must be a list")
        if not entries:
            return []
        totals: dict[str, list[Decimal]] = {}
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each financial ledger line must be an object")
            account = _identifier(entry.get("account_code"), "ledger account_code")
            currency = entry.get("currency")
            if not isinstance(currency, str) or not re.fullmatch(
                r"[A-Z0-9]{2,12}", currency
            ):
                raise ValueError("ledger currency must be an uppercase currency code")
            try:
                debit = Decimal(str(entry.get("debit", "0")))
                credit = Decimal(str(entry.get("credit", "0")))
            except (InvalidOperation, ValueError):
                raise ValueError(
                    "ledger debit/credit must be finite decimals"
                ) from None
            if (
                not debit.is_finite()
                or not credit.is_finite()
                or debit < 0
                or credit < 0
            ):
                raise ValueError("ledger amounts must be finite and non-negative")
            if (debit > 0) == (credit > 0):
                raise ValueError(
                    "each ledger line must contain exactly one positive side"
                )
            for amount in (debit, credit):
                if amount and (
                    -amount.as_tuple().exponent > 18 or amount.adjusted() > 19
                ):
                    raise ValueError("ledger precision exceeds NUMERIC(38,18)")
            totals.setdefault(currency, [Decimal(0), Decimal(0)])
            totals[currency][0] += debit
            totals[currency][1] += credit
            reference = entry.get("reference")
            if reference is not None and (
                not isinstance(reference, str) or len(reference) > 256
            ):
                raise ValueError("ledger reference must be at most 256 characters")
            normalized.append(
                {
                    "account_code": account,
                    "currency": currency,
                    "debit": debit,
                    "credit": credit,
                    "reference": reference,
                }
            )
        if any(debit != credit for debit, credit in totals.values()):
            raise ValueError("financial ledger journal must balance per currency")
        return normalized

    async def financial_balances(self, account_scope: str) -> list[dict[str, Any]]:
        """Return canonical debit/credit/net balances reconstructed from immutable lines."""
        scope = _identifier(account_scope, "account_scope")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT account_code,currency,
                          SUM(debit) AS debit,SUM(credit) AS credit
                   FROM aitos_oms_financial_ledger
                   WHERE account_scope=$1
                   GROUP BY account_code,currency
                   ORDER BY currency,account_code""",
                scope,
            )
        return [
            {
                "account_code": row["account_code"],
                "currency": row["currency"],
                "debit": row["debit"],
                "credit": row["credit"],
                "net_debit": row["debit"] - row["credit"],
            }
            for row in rows
        ]

    async def financial_trade_ledger(
        self, account_scope: str, trade_id: str
    ) -> list[dict[str, Any]]:
        """Read immutable accounting lines for one scoped trade."""
        scope = _identifier(account_scope, "account_scope")
        trade = _identifier(trade_id, "trade_id")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT event_id,line_no,account_code,currency,debit,credit,
                          reference,recorded_at
                   FROM aitos_oms_financial_ledger
                   WHERE account_scope=$1 AND trade_id=$2
                   ORDER BY recorded_at,event_id,line_no""",
                scope,
                trade,
            )
        return [dict(row) for row in rows]

    async def writer_generation(self, account_scope: str) -> int:
        """Return the current primary-store fencing generation (zero if new)."""
        scope = _identifier(account_scope, "account_scope")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT generation FROM aitos_oms_writer_fences WHERE account_scope=$1",
                scope,
            )
        return int(row["generation"]) if row else 0

    async def get_legacy_import_payload(
        self,
        account_scope: str,
        source_table: str,
        source_fingerprint: str,
        source_occurrence: int,
        expected_trade_id: str,
        expected_updated_at: str,
    ) -> str:
        """Read exact legacy JSON only after validating its durable SHA-256 evidence."""
        scope = _identifier(account_scope, "account scope")
        table = _identifier(source_table, "legacy source table")
        fingerprint = _identifier(source_fingerprint, "source fingerprint")
        trade_id = _identifier(expected_trade_id, "legacy trade ID")
        if not isinstance(expected_updated_at, str) or not expected_updated_at:
            raise ValueError("legacy source timestamp is required")
        if (
            len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
            or type(source_occurrence) is not int
            or source_occurrence < 0
        ):
            raise ValueError("invalid legacy import evidence identity")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT trade_id,source_updated_at,raw_payload,raw_payload_sha256
                FROM aitos_oms_legacy_imports
                WHERE account_scope=$1 AND source_table=$2
                  AND source_fingerprint=$3 AND source_occurrence=$4
                """,
                scope,
                table,
                fingerprint,
                source_occurrence,
            )
        if row is None:
            raise RuntimeError("canonical legacy import evidence is missing")
        raw_payload = row["raw_payload"]
        if (
            row["trade_id"] != trade_id
            or row["source_updated_at"].isoformat() != expected_updated_at
        ):
            raise RuntimeError("canonical legacy import evidence identity mismatch")
        expected_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, row["raw_payload_sha256"]):
            raise RuntimeError("canonical legacy import evidence SHA-256 mismatch")
        return raw_payload

    async def list_aggregate_ids(self, account_scope: str) -> list[str]:
        """List aggregate identities with committed canonical history for one scope."""
        scope = _identifier(account_scope, "account_scope")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT aggregate_id FROM aitos_oms_aggregate_heads
                   WHERE account_scope=$1 ORDER BY aggregate_id""",
                scope,
            )
        return [str(row["aggregate_id"]) for row in rows]

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
                scope,
                aggregate,
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

    async def claim_outbox_leased(
        self,
        owner_id: str,
        *,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Atomically lease pending projection records to one worker.

        The lease survives transaction completion and expires after a worker
        crash. Competing workers cannot claim the same live lease; expired
        claims are eligible for recovery without deleting canonical events.
        """
        owner = _identifier(owner_id, "outbox worker_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("outbox lease_seconds must be between 1 and 3600")
        token = str(uuid.uuid4())
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """WITH candidates AS (
                       SELECT account_scope,aggregate_id,revision
                       FROM aitos_oms_outbox
                       WHERE projected_at IS NULL
                         AND (claim_until IS NULL OR claim_until <= now())
                       ORDER BY created_at,account_scope,aggregate_id,revision
                       LIMIT $1
                       FOR UPDATE SKIP LOCKED
                   )
                   UPDATE aitos_oms_outbox AS o
                   SET claim_owner=$2,claim_token=$3::uuid,
                       claim_until=now()+($4 * interval '1 second'),
                       attempts=o.attempts+1
                   FROM candidates AS c
                   JOIN aitos_oms_events AS e
                     USING(account_scope,aggregate_id,revision)
                   WHERE o.account_scope=c.account_scope
                     AND o.aggregate_id=c.aggregate_id
                     AND o.revision=c.revision
                   RETURNING o.account_scope,o.aggregate_id,o.revision,
                             o.attempts,o.claim_owner,o.claim_token,
                             e.event_id,e.payload::text AS payload_json,
                             e.event_hash""",
                limit,
                owner,
                token,
                lease_seconds,
            )
        return [dict(row) for row in rows]

    async def acknowledge_outbox_claim(
        self,
        record: dict[str, Any],
        *,
        owner_id: str,
        claim_token: str,
    ) -> None:
        """Acknowledge only the exact leased event after projection succeeds."""
        if not isinstance(record, dict):
            raise TypeError("outbox record must be an object")
        owner = _identifier(owner_id, "outbox worker_id")
        try:
            token = str(uuid.UUID(claim_token))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("valid outbox claim token is required") from None
        scope = _identifier(record.get("account_scope"), "account_scope")
        aggregate = _identifier(record.get("aggregate_id"), "aggregate_id")
        revision = record.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("outbox revision must be positive")
        event_hash = record.get("event_hash")
        if not isinstance(event_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", event_hash):
            raise ValueError("outbox event hash is invalid")
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                """UPDATE aitos_oms_outbox
                   SET projected_at=now(),claim_owner=NULL,claim_token=NULL,
                       claim_until=NULL,last_error=NULL
                   WHERE account_scope=$1 AND aggregate_id=$2 AND revision=$3
                     AND claim_owner=$4 AND claim_token=$5::uuid
                     AND projected_at IS NULL
                     AND EXISTS (
                       SELECT 1 FROM aitos_oms_events e
                       WHERE e.account_scope=$1 AND e.aggregate_id=$2
                         AND e.revision=$3 AND e.event_hash=$6
                     )""",
                scope,
                aggregate,
                revision,
                owner,
                token,
                event_hash,
            )
            if result != "UPDATE 1":
                raise OMSConflict("outbox lease is stale, expired, or already acknowledged")

    async def release_outbox_claim(
        self,
        record: dict[str, Any],
        *,
        owner_id: str,
        claim_token: str,
        error: str,
    ) -> None:
        """Release a failed projection for retry while retaining bounded diagnostics."""
        if not isinstance(record, dict):
            raise TypeError("outbox record must be an object")
        owner = _identifier(owner_id, "outbox worker_id")
        try:
            token = str(uuid.UUID(claim_token))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("valid outbox claim token is required") from None
        scope = _identifier(record.get("account_scope"), "account_scope")
        aggregate = _identifier(record.get("aggregate_id"), "aggregate_id")
        revision = record.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("outbox revision must be positive")
        detail = str(error).replace("\\x00", "")[:2000] or "projection failed"
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                """UPDATE aitos_oms_outbox
                   SET claim_owner=NULL,claim_token=NULL,claim_until=NULL,
                       last_error=$6
                   WHERE account_scope=$1 AND aggregate_id=$2 AND revision=$3
                     AND claim_owner=$4 AND claim_token=$5::uuid
                     AND projected_at IS NULL""",
                scope,
                aggregate,
                revision,
                owner,
                token,
                detail,
            )
            if result != "UPDATE 1":
                raise OMSConflict("cannot release an outbox lease not owned by this worker")

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
