import json

import pytest

from scripts.import_clickhouse_state_to_postgres_oms import _manifest_row
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
