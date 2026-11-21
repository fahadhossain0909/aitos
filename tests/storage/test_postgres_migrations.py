from __future__ import annotations

import asyncio

import pytest

from aitos.storage.postgres_migrations import (
    MIGRATIONS,
    _REQUIRED_COLUMNS,
    verify_postgres_schema,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, *, migration_rows=None, missing_columns=None):
        self.migration_rows = migration_rows
        self.missing_columns = missing_columns or set()
        self.read_only_transaction = False

    def transaction(self, *, readonly=False):
        assert readonly is True
        self.read_only_transaction = True
        return _Transaction()

    async def fetch(self, query, *args):
        if "SELECT version, name, checksum FROM aitos_schema_migrations" in query:
            return self.migration_rows
        if "information_schema.columns" in query:
            table = args[0]
            return [
                {"column_name": column}
                for column in sorted(_REQUIRED_COLUMNS[table] - self.missing_columns)
            ]
        raise AssertionError(f"unexpected SQL in read-only schema verifier: {query}")

    async def execute(self, *args):
        raise AssertionError("read-only schema verification must not execute writes")


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _migration_rows():
    return [
        {
            "version": migration.version,
            "name": migration.name,
            "checksum": migration.checksum,
        }
        for migration in MIGRATIONS
    ]


def test_schema_verification_uses_read_only_transaction_and_no_ddl():
    connection = _Connection(migration_rows=_migration_rows())

    version = asyncio.run(verify_postgres_schema(_Pool(connection)))

    assert version == 1
    assert connection.read_only_transaction is True


def test_schema_verification_fails_closed_on_missing_migration():
    connection = _Connection(migration_rows=[])

    with pytest.raises(RuntimeError, match="not fully migrated"):
        asyncio.run(verify_postgres_schema(_Pool(connection)))


def test_schema_verification_fails_closed_on_checksum_drift():
    rows = _migration_rows()
    rows[0] = {**rows[0], "checksum": "0" * 64}
    connection = _Connection(migration_rows=rows)

    with pytest.raises(RuntimeError, match="history mismatch"):
        asyncio.run(verify_postgres_schema(_Pool(connection)))


def test_schema_verification_fails_closed_on_missing_required_column():
    connection = _Connection(
        migration_rows=_migration_rows(),
        missing_columns={"payload"},
    )

    with pytest.raises(RuntimeError, match="schema drift"):
        asyncio.run(verify_postgres_schema(_Pool(connection)))
