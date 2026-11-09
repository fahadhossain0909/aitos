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
)


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
}


async def apply_postgres_migrations(pool) -> int:
    """Apply all pending migrations and return the current schema version.

    The advisory lock, DDL and version insert share one transaction. Existing
    unversioned tables created by earlier AITOS releases are adopted by the
    idempotent v1 migration and validated before the transaction commits.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
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
