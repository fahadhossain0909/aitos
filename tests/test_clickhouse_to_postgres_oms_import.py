import asyncio
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from scripts.import_clickhouse_state_to_postgres_oms import _manifest_row, run
from scripts.migrate_scoped_clickhouse_state import CLOSED_SOURCE, RUNTIME_SOURCE


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
