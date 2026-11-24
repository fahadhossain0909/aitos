"""Versioned, transactional PostgreSQL schema migrations for AITOS adapters.

Migrations are serialized with a PostgreSQL advisory transaction lock. A
migration and its version/checksum record commit atomically, so a process crash
cannot leave a migration marked complete when its DDL was rolled back.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

_MIGRATION_LOCK_ID = 4_149_720_261_246_01
_MIGRATION_TABLE = "aitos_schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        source = "\n-- statement boundary --\n".join(
            statement.strip() for statement in self.statements
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration(
        version=1,
        name="operational_and_market_records",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS aitos_market_records (
                record_id BIGSERIAL PRIMARY KEY,
                record_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                event_time TIMESTAMPTZ NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                payload JSONB NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_aitos_market_recent
            ON aitos_market_records (record_type, entity_id, event_time DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS aitos_operational_records (
                record_id BIGSERIAL PRIMARY KEY,
                record_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                recorded_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_aitos_operational_records_entity
            ON aitos_operational_records (record_type, entity_id, recorded_at)
            """,
        ),
    ),
    Migration(
        version=2,
        name="canonical_scope_keyed_oms",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_writer_fences (
                account_scope TEXT PRIMARY KEY,
                writer_id TEXT NOT NULL,
                generation BIGINT NOT NULL CHECK (generation > 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_aggregate_heads (
                account_scope TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                revision BIGINT NOT NULL CHECK (revision >= 0),
                event_hash CHAR(64) NOT NULL,
                PRIMARY KEY (account_scope, aggregate_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_events (
                account_scope TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                revision BIGINT NOT NULL CHECK (revision > 0),
                event_id TEXT NOT NULL,
                writer_id TEXT NOT NULL,
                generation BIGINT NOT NULL CHECK (generation > 0),
                payload JSONB NOT NULL,
                previous_hash CHAR(64) NOT NULL,
                event_hash CHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (account_scope, aggregate_id, revision),
                UNIQUE (account_scope, event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_outbox (
                account_scope TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                revision BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                projected_at TIMESTAMPTZ,
                PRIMARY KEY (account_scope, aggregate_id, revision),
                FOREIGN KEY (account_scope, aggregate_id, revision)
                  REFERENCES aitos_oms_events(account_scope, aggregate_id, revision)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_aitos_oms_outbox_pending
            ON aitos_oms_outbox (created_at, account_scope, aggregate_id, revision)
            WHERE projected_at IS NULL
            """,
        ),
    ),
    Migration(
        version=3,
        name="clickhouse_legacy_import_evidence",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_legacy_imports (
                account_scope TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_fingerprint CHAR(64) NOT NULL,
                source_occurrence BIGINT NOT NULL CHECK (source_occurrence >= 0),
                trade_id TEXT NOT NULL,
                source_updated_at TIMESTAMPTZ NOT NULL,
                raw_payload TEXT NOT NULL,
                raw_payload_sha256 CHAR(64) NOT NULL,
                imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (
                    account_scope, source_table, source_fingerprint, source_occurrence
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_aitos_oms_legacy_trade
            ON aitos_oms_legacy_imports (account_scope, trade_id, source_updated_at)
            """,
            """
            CREATE OR REPLACE FUNCTION aitos_reject_oms_event_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'canonical OMS event journal is append-only';
            END;
            $$
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_events_no_update ON aitos_oms_events
            """,
            """
            CREATE TRIGGER aitos_oms_events_no_update
            BEFORE UPDATE ON aitos_oms_events
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_oms_event_mutation()
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_events_no_delete ON aitos_oms_events
            """,
            """
            CREATE TRIGGER aitos_oms_events_no_delete
            BEFORE DELETE ON aitos_oms_events
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_oms_event_mutation()
            """,
            """
            CREATE OR REPLACE FUNCTION aitos_reject_legacy_import_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'canonical legacy import evidence is append-only';
            END;
            $$
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_legacy_imports_no_update
            ON aitos_oms_legacy_imports
            """,
            """
            CREATE TRIGGER aitos_oms_legacy_imports_no_update
            BEFORE UPDATE ON aitos_oms_legacy_imports
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_legacy_import_mutation()
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_legacy_imports_no_delete
            ON aitos_oms_legacy_imports
            """,
            """
            CREATE TRIGGER aitos_oms_legacy_imports_no_delete
            BEFORE DELETE ON aitos_oms_legacy_imports
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_legacy_import_mutation()
            """,
        ),
    ),
    Migration(
        version=4,
        name="canonical_oms_financial_ledger",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS aitos_oms_financial_ledger (
                account_scope TEXT NOT NULL,
                event_id TEXT NOT NULL,
                line_no INTEGER NOT NULL CHECK (line_no >= 0),
                trade_id TEXT NOT NULL,
                account_code TEXT NOT NULL,
                currency TEXT NOT NULL,
                debit NUMERIC(38, 18) NOT NULL DEFAULT 0 CHECK (debit >= 0),
                credit NUMERIC(38, 18) NOT NULL DEFAULT 0 CHECK (credit >= 0),
                reference TEXT,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (account_scope, event_id, line_no),
                FOREIGN KEY (account_scope, event_id)
                  REFERENCES aitos_oms_events(account_scope, event_id),
                CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_aitos_oms_financial_trade
            ON aitos_oms_financial_ledger
              (account_scope, trade_id, currency, recorded_at)
            """,
            """
            CREATE OR REPLACE FUNCTION aitos_reject_oms_financial_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $aitos$
            BEGIN
                RAISE EXCEPTION 'canonical OMS financial ledger is append-only';
            END;
            $aitos$
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_financial_no_update
            ON aitos_oms_financial_ledger
            """,
            """
            CREATE TRIGGER aitos_oms_financial_no_update
            BEFORE UPDATE ON aitos_oms_financial_ledger
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_oms_financial_mutation()
            """,
            """
            DROP TRIGGER IF EXISTS aitos_oms_financial_no_delete
            ON aitos_oms_financial_ledger
            """,
            """
            CREATE TRIGGER aitos_oms_financial_no_delete
            BEFORE DELETE ON aitos_oms_financial_ledger
            FOR EACH ROW EXECUTE FUNCTION aitos_reject_oms_financial_mutation()
            """,
        ),
    ),
)


async def verify_postgres_schema(pool) -> int:
    """Validate an existing schema using read-only transactions and no DDL."""
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        rows = await conn.fetch(
            "SELECT version, name, checksum FROM aitos_schema_migrations"
        )
        applied = {int(row["version"]): row for row in rows}
        known = {migration.version for migration in MIGRATIONS}
        unknown = set(applied) - known
        if unknown:
            raise RuntimeError(
                "PostgreSQL schema is newer than this AITOS build; "
                "unknown migration versions: "
                + ", ".join(str(version) for version in sorted(unknown))
            )

        for migration in MIGRATIONS:
            previous = applied.get(migration.version)
            if previous is None:
                raise RuntimeError(
                    "PostgreSQL schema is not fully migrated; "
                    f"missing version {migration.version}"
                )
            if (
                previous["name"] != migration.name
                or previous["checksum"] != migration.checksum
            ):
                raise RuntimeError(
                    "PostgreSQL migration history mismatch at version "
                    f"{migration.version}; refusing unsafe read"
                )

        for table, expected in _REQUIRED_COLUMNS.items():
            columns = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = $1
                """,
                table,
            )
            actual = {row["column_name"] for row in columns}
            missing = expected - actual
            if missing:
                raise RuntimeError(
                    f"PostgreSQL schema drift in {table}; missing columns: "
                    + ", ".join(sorted(missing))
                )

    return max((migration.version for migration in MIGRATIONS), default=0)


_REQUIRED_COLUMNS = {
    "aitos_market_records": {
        "record_id",
        "record_type",
        "entity_id",
        "idempotency_key",
        "event_time",
        "recorded_at",
        "payload",
    },
    "aitos_operational_records": {
        "record_id",
        "record_type",
        "entity_id",
        "idempotency_key",
        "recorded_at",
        "payload",
    },
    "aitos_oms_writer_fences": {"account_scope", "writer_id", "generation"},
    "aitos_oms_aggregate_heads": {
        "account_scope",
        "aggregate_id",
        "revision",
        "event_hash",
    },
    "aitos_oms_events": {
        "account_scope",
        "aggregate_id",
        "revision",
        "event_id",
        "writer_id",
        "generation",
        "payload",
        "previous_hash",
        "event_hash",
    },
    "aitos_oms_outbox": {
        "account_scope",
        "aggregate_id",
        "revision",
        "projected_at",
    },
    "aitos_oms_financial_ledger": {
        "account_scope",
        "event_id",
        "line_no",
        "trade_id",
        "account_code",
        "currency",
        "debit",
        "credit",
        "recorded_at",
    },\n    "aitos_oms_legacy_imports": {
        "account_scope",
        "source_table",
        "source_fingerprint",
        "source_occurrence",
        "trade_id",
        "source_updated_at",
        "raw_payload",
        "raw_payload_sha256",
        "imported_at",
    },
}


async def apply_postgres_migrations(pool) -> int:
    """Apply all pending migrations and return the current schema version.

    The advisory lock, DDL and version insert share one transaction. Existing
    unversioned tables created by earlier AITOS releases are adopted by the
    idempotent v1 migration and validated before the transaction commits.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_ID)
        await conn.execute(
            """
                CREATE TABLE IF NOT EXISTS aitos_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
        )
        rows = await conn.fetch(
            "SELECT version, name, checksum FROM aitos_schema_migrations"
        )
        applied = {int(row["version"]): row for row in rows}
        known_versions = {migration.version for migration in MIGRATIONS}
        unknown_versions = set(applied) - known_versions
        if unknown_versions:
            raise RuntimeError(
                "PostgreSQL schema is newer than this AITOS build; "
                "unknown migration versions: "
                + ", ".join(str(version) for version in sorted(unknown_versions))
            )

        for migration in MIGRATIONS:
            previous = applied.get(migration.version)
            if previous is not None:
                if (
                    previous["name"] != migration.name
                    or previous["checksum"] != migration.checksum
                ):
                    raise RuntimeError(
                        "PostgreSQL migration history mismatch at version "
                        f"{migration.version}; refusing unsafe startup"
                    )
                continue

            for statement in migration.statements:
                await conn.execute(statement)
            await conn.execute(
                """
                    INSERT INTO aitos_schema_migrations (version, name, checksum)
                    VALUES ($1, $2, $3)
                    """,
                migration.version,
                migration.name,
                migration.checksum,
            )

        for table, expected in _REQUIRED_COLUMNS.items():
            rows = await conn.fetch(
                """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = $1
                    """,
                table,
            )
            actual = {row["column_name"] for row in rows}
            missing = expected - actual
            if missing:
                raise RuntimeError(
                    f"PostgreSQL schema drift in {table}; missing columns: "
                    + ", ".join(sorted(missing))
                )

        return max((migration.version for migration in MIGRATIONS), default=0)
