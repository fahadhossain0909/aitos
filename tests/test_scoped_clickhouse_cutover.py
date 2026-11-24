import pytest

from scripts.migrate_scoped_clickhouse_state import (
    CLOSED_BACKUP,
    CLOSED_SOURCE,
    CLOSED_TARGET,
    RUNTIME_BACKUP,
    RUNTIME_SOURCE,
    RUNTIME_TARGET,
    _verified_cutover,
)


class Result:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    def __init__(self, *, engine="Atomic", tables=()):
        self.engine = engine
        self.tables = set(tables) | {
            RUNTIME_SOURCE,
            CLOSED_SOURCE,
            RUNTIME_TARGET,
            CLOSED_TARGET,
        }
        self.commands = []

    def query(self, sql):
        if "system.databases" in sql:
            return Result([(self.engine,)])
        if "system.tables" in sql:
            return Result([(name,) for name in self.tables])
        if "count()" in sql:
            return Result([(7,)])
        raise AssertionError(sql)

    def command(self, sql):
        self.commands.append(sql)


def test_verified_cutover_renames_both_tables_and_retains_backups():
    client = FakeClient()
    _verified_cutover(client)
    assert len(client.commands) == 1
    rename = client.commands[0]
    assert f"{RUNTIME_SOURCE} TO {RUNTIME_BACKUP}" in rename
    assert f"{RUNTIME_TARGET} TO {RUNTIME_SOURCE}" in rename
    assert f"{CLOSED_SOURCE} TO {CLOSED_BACKUP}" in rename
    assert f"{CLOSED_TARGET} TO {CLOSED_SOURCE}" in rename


def test_verified_cutover_rejects_non_atomic_database():
    client = FakeClient(engine="MergeTree")
    with pytest.raises(RuntimeError, match="Atomic"):
        _verified_cutover(client)
    assert client.commands == []


def test_verified_cutover_refuses_to_overwrite_existing_backup():
    client = FakeClient(tables={RUNTIME_BACKUP})
    with pytest.raises(RuntimeError, match="backup already exists"):
        _verified_cutover(client)
    assert client.commands == []
