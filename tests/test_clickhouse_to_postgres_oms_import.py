import asyncio
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from scripts.import_clickhouse_state_to_postgres_oms import (
    _manifest_row,
    _scoped_payload_hash,
    run,
)
from scripts.migrate_scoped_clickhouse_state import (
    CLOSED_SOURCE,
    RUNTIME_SOURCE,
    source_fingerprint,
)


def test_runtime_manifest_preserves_raw_payload_and_identity():
    raw = json.dumps(
        {
            "trade_id": "trade-1",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "quantity": "0.125",
            "account_scope": "paper:account-a",
        },
        separators=(",", ":"),
    )
    item = _manifest_row(
        RUNTIME_SOURCE,
        ("trade-1", "BTCUSDT", "position_opened", raw, "2026-11-24T10:00:00Z"),
        0,
    )
    assert item["trade_id"] == "trade-1"
    assert item["raw_payload"] == raw
    assert item["payload"]["quantity"] == "0.125"
    assert item["source_occurrence"] == 0
    assert len(item["raw_payload_sha256"]) == 64


def test_closed_manifest_rejects_nonterminal_record():
    raw = json.dumps({"trade_id": "trade-2", "state": "position_opened"})
    with pytest.raises(ValueError, match="nonterminal"):
        _manifest_row(
            CLOSED_SOURCE,
            ("trade-2", raw, "2026-11-24T10:00:00Z"),
            0,
        )


def test_manifest_rejects_row_payload_identity_mismatch():
    raw = json.dumps({"trade_id": "other-trade", "symbol": "BTCUSDT"})
    with pytest.raises(ValueError, match="identity mismatch"):
        _manifest_row(
            RUNTIME_SOURCE,
            ("trade-1", "BTCUSDT", "", raw, "2026-11-24T10:00:00Z"),
            0,
        )


def _fake_clickhouse_module(runtime_rows, closed_rows):
    class FakeClient:
        closed = False

        def query(self, query):
            if "system.columns" in query:
                return SimpleNamespace(
                    result_rows=[
                        ("trade_runtime_state", "trade_id"),
                        ("paper_closed_trade_ledger", "trade_id"),
                    ]
                )
            raise AssertionError(f"unexpected ClickHouse query: {query}")

        @contextmanager
        def query_row_block_stream(self, query):
            if "FROM trade_runtime_state" in query:
                yield iter([runtime_rows])
            else:
                yield iter([closed_rows])

        def close(self):
            self.closed = True

    client = FakeClient()
    module = ModuleType("clickhouse_connect")
    module.get_client = lambda **kwargs: client
    return module, client


def test_streaming_dry_run_does_not_require_or_mutate_postgres(tmp_path, monkeypatch):
    runtime = json.dumps(
        {
            "trade_id": "trade-open",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "account_scope": "paper:one",
            "quantity": "0.01",
        },
        separators=(",", ":"),
    )
    closed = json.dumps(
        {
            "trade_id": "trade-closed",
            "state": "position_closed",
            "account_scope": "paper:one",
            "pnl": "1.25",
        },
        separators=(",", ":"),
    )
    module, client = _fake_clickhouse_module(
        [("trade-open", "BTCUSDT", "position_opened", runtime, "2026-11-24T10:00:00Z")],
        [("trade-closed", closed, "2026-11-24T11:00:00Z")],
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", module)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    output = tmp_path / "manifest.jsonl"
    result = asyncio.run(
        run(
            SimpleNamespace(
                apply=False,
                confirm_writers_stopped=False,
                confirm_backup_retained=False,
                scope_map={},
                manifest_out=output,
            )
        )
    )
    assert result["mode"] == "dry-run"
    assert result["writes"] == 0
    assert result["postgres_connected"] is False
    assert result["runtime_rows"] == 1
    assert result["closed_rows"] == 1
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["raw_payload"] == runtime
    assert rows[1]["raw_payload"] == closed
    assert client.closed is True


def test_streaming_import_refuses_ambiguous_same_timestamp_snapshots(
    tmp_path, monkeypatch
):
    first = json.dumps(
        {
            "trade_id": "trade-ambiguous",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "account_scope": "paper:one",
            "quantity": "0.01",
        },
        separators=(",", ":"),
    )
    second = json.dumps(
        {
            "trade_id": "trade-ambiguous",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "account_scope": "paper:one",
            "quantity": "0.02",
        },
        separators=(",", ":"),
    )
    module, _ = _fake_clickhouse_module(
        [
            ("trade-ambiguous", "BTCUSDT", "position_opened", first, "2026-11-24T10:00:00Z"),
            ("trade-ambiguous", "BTCUSDT", "position_opened", second, "2026-11-24T10:00:00Z"),
        ],
        [],
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", module)
    output = tmp_path / "manifest.jsonl"
    with pytest.raises(RuntimeError, match="ambiguous same-timestamp"):
        asyncio.run(
            run(
                SimpleNamespace(
                    apply=False,
                    confirm_writers_stopped=False,
                    confirm_backup_retained=False,
                    scope_map={},
                    manifest_out=output,
                )
            )
        )
    assert output.with_suffix(".jsonl.partial").is_file()


def test_streaming_dry_run_reads_already_scope_keyed_cutover_tables(
    tmp_path, monkeypatch
):
    runtime = json.dumps(
        {
            "trade_id": "scoped-open",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "quantity": "0.02",
        },
        separators=(",", ":"),
    )
    closed = json.dumps(
        {"trade_id": "scoped-closed", "state": "position_closed", "pnl": "2"},
        separators=(",", ":"),
    )
    runtime_stamp = "2026-11-24T10:00:00Z"
    closed_stamp = "2026-11-24T11:00:00Z"
    runtime_item = _manifest_row(
        RUNTIME_SOURCE,
        ("scoped-open", "BTCUSDT", "position_opened", runtime, runtime_stamp),
        0,
    )
    closed_item = _manifest_row(
        CLOSED_SOURCE,
        ("scoped-closed", closed, closed_stamp),
        0,
    )
    runtime_fp = source_fingerprint(
        RUNTIME_SOURCE, "scoped-open", runtime, runtime_stamp
    )
    closed_fp = source_fingerprint(
        CLOSED_SOURCE, "scoped-closed", closed, closed_stamp
    )
    scope = "paper:cutover"

    class ScopedClient:
        closed = False

        def query(self, query):
            if "system.columns" in query:
                return SimpleNamespace(
                    result_rows=[
                        (RUNTIME_SOURCE, "account_scope"),
                        (RUNTIME_SOURCE, "trade_id"),
                        (RUNTIME_SOURCE + "_legacy_pre_scope_v1", "trade_id"),
                        (CLOSED_SOURCE, "account_scope"),
                        (CLOSED_SOURCE, "trade_id"),
                        (CLOSED_SOURCE + "_legacy_pre_scope_v1", "trade_id"),
                    ]
                )
            if "SELECT count()" in query:
                return SimpleNamespace(result_rows=[(1,)])
            raise AssertionError(f"unexpected ClickHouse query: {query}")

        @contextmanager
        def query_row_block_stream(self, query):
            if f"FROM {RUNTIME_SOURCE}" in query:
                yield iter(
                    [[
                        (
                            scope,
                            "scoped-open",
                            "BTCUSDT",
                            "position_opened",
                            runtime,
                            runtime_stamp,
                            runtime_fp,
                            0,
                            _scoped_payload_hash(runtime_item["payload"], scope),
                        )
                    ]]
                )
            else:
                yield iter(
                    [[
                        (
                            scope,
                            "scoped-closed",
                            closed,
                            closed_stamp,
                            closed_fp,
                            0,
                            _scoped_payload_hash(closed_item["payload"], scope),
                        )
                    ]]
                )

        def close(self):
            self.closed = True

    client = ScopedClient()
    module = ModuleType("clickhouse_connect")
    module.get_client = lambda **kwargs: client
    monkeypatch.setitem(sys.modules, "clickhouse_connect", module)
    output = tmp_path / "scoped-manifest.jsonl"
    result = asyncio.run(
        run(
            SimpleNamespace(
                apply=False,
                confirm_writers_stopped=False,
                confirm_backup_retained=False,
                scope_map={},
                manifest_out=output,
            )
        )
    )
    assert result["runtime_rows"] == 1
    assert result["closed_rows"] == 1
    manifest = [json.loads(line) for line in output.read_text().splitlines()]
    assert {row["account_scope"] for row in manifest} == {scope}
    assert {row["source_fingerprint"] for row in manifest} == {
        runtime_fp,
        closed_fp,
    }
