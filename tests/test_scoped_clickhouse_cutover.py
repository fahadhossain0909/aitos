import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts.migrate_scoped_clickhouse_state import (
    CLOSED_BACKUP,
    CLOSED_SOURCE,
    CLOSED_TARGET,
    RUNTIME_BACKUP,
    RUNTIME_SOURCE,
    RUNTIME_TARGET,
    _verified_cutover,
    _verified_rollback,
    source_fingerprint,
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


class RollbackClient:
    def __init__(self, *, mismatch=False):
        self.engine = "Atomic"
        self.tables = {
            RUNTIME_SOURCE,
            CLOSED_SOURCE,
            RUNTIME_BACKUP,
            CLOSED_BACKUP,
        }
        self.commands = []
        stamp = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
        runtime_raw = json.dumps(
            {
                "trade_id": "trade-open",
                "symbol": "BTCUSDT",
                "state": "position_opened",
                "quantity": "0.01",
            },
            separators=(",", ":"),
        )
        closed_raw = json.dumps(
            {"trade_id": "trade-closed", "state": "position_closed", "pnl": "1.25"},
            separators=(",", ":"),
        )
        self.datasets = {
            RUNTIME_BACKUP: [
                ("trade-open", "BTCUSDT", "position_opened", runtime_raw, stamp),
                ("trade-open", "BTCUSDT", "position_opened", runtime_raw, stamp),
            ],
            CLOSED_BACKUP: [("trade-closed", closed_raw, stamp)],
        }
        runtime_fp = source_fingerprint(
            RUNTIME_SOURCE, "trade-open", runtime_raw, stamp
        )
        closed_fp = source_fingerprint(
            CLOSED_SOURCE, "trade-closed", closed_raw, stamp
        )
        runtime_payload = json.loads(runtime_raw)
        runtime_payload["account_scope"] = "paper:account-a"
        runtime_encoded = json.dumps(
            runtime_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
        closed_payload = json.loads(closed_raw)
        closed_payload["account_scope"] = "paper:account-a"
        closed_encoded = json.dumps(
            closed_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
        runtime_hash = hashlib.sha256(runtime_encoded.encode()).hexdigest()
        closed_hash = hashlib.sha256(closed_encoded.encode()).hexdigest()
        self.datasets[RUNTIME_SOURCE] = [
            (
                "paper:account-a",
                "trade-open",
                "BTCUSDT",
                "position_opened",
                runtime_encoded,
                runtime_raw,
                stamp,
                runtime_fp,
                occurrence,
                runtime_hash,
            )
            for occurrence in (0, 1)
        ]
        self.datasets[CLOSED_SOURCE] = [
            (
                "paper:account-a",
                "trade-closed",
                closed_encoded,
                closed_raw,
                stamp,
                closed_fp,
                0,
                closed_hash,
            )
        ]
        if mismatch:
            row = list(self.datasets[RUNTIME_SOURCE][1])
            row[5] = row[5].replace('"0.01"', '"0.02"')
            self.datasets[RUNTIME_SOURCE][1] = tuple(row)

    def query(self, sql):
        if "system.databases" in sql:
            return Result([(self.engine,)])
        if "system.tables" in sql:
            return Result([(name,) for name in sorted(self.tables)])
        for table in (
            RUNTIME_SOURCE,
            CLOSED_SOURCE,
            RUNTIME_BACKUP,
            CLOSED_BACKUP,
            RUNTIME_TARGET,
            CLOSED_TARGET,
        ):
            if f"FROM {table}" in sql:
                return Result(self.datasets.get(table, []))
        raise AssertionError(sql)

    def command(self, sql):
        self.commands.append(sql)
        if len(self.commands) == 1:
            self.tables.remove(RUNTIME_SOURCE)
            self.tables.remove(RUNTIME_BACKUP)
            self.tables.remove(CLOSED_SOURCE)
            self.tables.remove(CLOSED_BACKUP)
            self.tables.update({RUNTIME_SOURCE, CLOSED_SOURCE, RUNTIME_TARGET, CLOSED_TARGET})
            self.datasets[RUNTIME_TARGET] = self.datasets.pop(RUNTIME_SOURCE)
            self.datasets[RUNTIME_SOURCE] = self.datasets.pop(RUNTIME_BACKUP)
            self.datasets[CLOSED_TARGET] = self.datasets.pop(CLOSED_SOURCE)
            self.datasets[CLOSED_SOURCE] = self.datasets.pop(CLOSED_BACKUP)
        elif len(self.commands) == 2:
            # Restore cutover layout if post-rename verification fails.
            self.tables.remove(RUNTIME_SOURCE)
            self.tables.remove(CLOSED_SOURCE)
            self.tables.remove(RUNTIME_TARGET)
            self.tables.remove(CLOSED_TARGET)
            self.tables.update({RUNTIME_SOURCE, CLOSED_SOURCE, RUNTIME_BACKUP, CLOSED_BACKUP})
            self.datasets[RUNTIME_BACKUP] = self.datasets.pop(RUNTIME_SOURCE)
            self.datasets[RUNTIME_SOURCE] = self.datasets.pop(RUNTIME_TARGET)
            self.datasets[CLOSED_BACKUP] = self.datasets.pop(CLOSED_SOURCE)
            self.datasets[CLOSED_SOURCE] = self.datasets.pop(CLOSED_TARGET)


def test_verified_rollback_restores_legacy_names_after_exact_occurrence_parity():
    client = RollbackClient()
    result = _verified_rollback(
        client, writers_quiesced=True, backup_confirmed=True
    )

    assert result == {
        "mode": "rollback-and-verified",
        "runtime_rows": 2,
        "closed_rows": 1,
        "legacy_tables_restored": True,
        "scoped_tables_retained": [RUNTIME_TARGET, CLOSED_TARGET],
    }
    assert len(client.commands) == 1
    assert RUNTIME_SOURCE in client.tables
    assert CLOSED_SOURCE in client.tables
    assert RUNTIME_TARGET in client.tables
    assert CLOSED_TARGET in client.tables


def test_verified_rollback_requires_both_operator_confirmations():
    client = RollbackClient()
    with pytest.raises(RuntimeError, match="writer-stop"):
        _verified_rollback(
            client, writers_quiesced=True, backup_confirmed=False
        )
    assert client.commands == []


def test_verified_rollback_refuses_raw_payload_divergence_before_rename():
    client = RollbackClient(mismatch=True)
    with pytest.raises(RuntimeError, match="rollback parity failed"):
        _verified_rollback(
            client, writers_quiesced=True, backup_confirmed=True
        )
    assert client.commands == []


def test_verified_rollback_refuses_non_atomic_database():
    client = RollbackClient()
    client.engine = "MergeTree"
    with pytest.raises(RuntimeError, match="Atomic"):
        _verified_rollback(
            client, writers_quiesced=True, backup_confirmed=True
        )
    assert client.commands == []
