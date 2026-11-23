import json
from datetime import datetime, timezone

import pytest

from scripts.migrate_scoped_clickhouse_state import (
    CLOSED_SOURCE,
    RUNTIME_SOURCE,
    _ddl,
    _manifest,
    prepare_record,
    source_fingerprint,
)


def runtime_row(*, scope=None, state="position_opened"):
    payload = {
        "trade_id": "trade-1",
        "symbol": "BTCUSDT",
        "state": state,
        "pnl": 0.0,
    }
    if scope is not None:
        payload["account_scope"] = scope
    return (
        "trade-1",
        "BTCUSDT",
        state,
        json.dumps(payload, separators=(",", ":")),
        datetime(2026, 11, 23, tzinfo=timezone.utc),
    )


def test_v2_tables_are_scope_keyed_append_only_and_keep_legacy_payload():
    runtime = _ddl("trade_runtime_state_v2", runtime=True)
    closed = _ddl("paper_closed_trade_ledger_v2", runtime=False)

    for ddl in (runtime, closed):
        assert "ENGINE = MergeTree" in ddl
        assert "ReplacingMergeTree" not in ddl
        assert "account_scope, trade_id, updated_at" in ddl
        assert "source_fingerprint FixedString(64)" in ddl
        assert "source_occurrence UInt64" in ddl
        assert "legacy_payload_json String" in ddl


def test_source_fingerprint_is_stable_but_source_table_specific():
    row = runtime_row()
    fp = source_fingerprint(RUNTIME_SOURCE, row[0], row[3], row[4])

    assert fp == source_fingerprint(RUNTIME_SOURCE, row[0], row[3], row[4])
    assert fp != source_fingerprint(CLOSED_SOURCE, row[0], row[3], row[4])


def test_legacy_unscoped_record_requires_explicit_row_fingerprint_mapping():
    row = runtime_row()
    fp = source_fingerprint(RUNTIME_SOURCE, row[0], row[3], row[4])

    with pytest.raises(ValueError, match="explicit valid account scope"):
        prepare_record(RUNTIME_SOURCE, row, scope_map={}, occurrence=0)

    fingerprint, record = prepare_record(
        RUNTIME_SOURCE, row, scope_map={fp: "paper:account-a"}, occurrence=0
    )
    assert fingerprint == fp
    assert record[0] == "paper:account-a"
    assert json.loads(record[4])["account_scope"] == "paper:account-a"
    assert record[5] == row[3]  # byte-for-byte legacy payload is retained
    assert record[8] == 0


def test_embedded_scope_is_preserved_and_mapping_cannot_override_it():
    row = runtime_row(scope="live:account-a")
    fp = source_fingerprint(RUNTIME_SOURCE, row[0], row[3], row[4])

    _, record = prepare_record(RUNTIME_SOURCE, row, scope_map={}, occurrence=0)
    assert record[0] == "live:account-a"

    with pytest.raises(ValueError, match="conflicts"):
        prepare_record(
            RUNTIME_SOURCE, row, scope_map={fp: "live:account-b"}, occurrence=0
        )


def test_runtime_identity_mismatch_is_rejected():
    row = list(runtime_row())
    payload = json.loads(row[3])
    payload["symbol"] = "ETHUSDT"
    row[3] = json.dumps(payload)

    with pytest.raises(ValueError, match="symbol does not match"):
        prepare_record(RUNTIME_SOURCE, tuple(row), scope_map={
            source_fingerprint(RUNTIME_SOURCE, row[0], row[3], row[4]): "paper:a"
        }, occurrence=0)


def test_closed_ledger_only_accepts_terminal_records():
    payload = {
        "trade_id": "trade-closed",
        "state": "position_opened",
        "pnl": 0.0,
    }
    row = (
        "trade-closed",
        json.dumps(payload),
        datetime(2026, 11, 23, tzinfo=timezone.utc),
    )
    fp = source_fingerprint(CLOSED_SOURCE, row[0], row[1], row[2])

    with pytest.raises(ValueError, match="nonterminal"):
        prepare_record(CLOSED_SOURCE, row, scope_map={fp: "paper:a"}, occurrence=0)


def test_manifest_counts_identical_physical_rows_for_lossless_occurrence_numbering():
    row = runtime_row()
    manifest = _manifest({RUNTIME_SOURCE: [row, row], CLOSED_SOURCE: []})

    assert len(manifest) == 1
    assert manifest[0]["occurrences"] == 2
    assert manifest[0]["embedded_account_scope"] is None
    assert manifest[0]["source_fingerprint"] == source_fingerprint(
        RUNTIME_SOURCE, row[0], row[3], row[4]
    )


def test_scope_mapping_is_bound_to_exact_source_fingerprint():
    row_a = runtime_row()
    row_b = list(runtime_row())
    row_b[3] = row_b[3].replace("trade-1", "trade-2")
    row_b[0] = "trade-2"
    fp_a = source_fingerprint(RUNTIME_SOURCE, row_a[0], row_a[3], row_a[4])

    with pytest.raises(ValueError, match="explicit valid account scope"):
        prepare_record(
            RUNTIME_SOURCE,
            tuple(row_b),
            scope_map={fp_a: "paper:a"},
            occurrence=0,
        )
