"""Embedded ClickHouse engine acceptance for the additive scope-keyed v2 schema."""

import json

import pytest

from scripts.migrate_scoped_clickhouse_state import (
    _ddl,
)

pytestmark = pytest.mark.integration


def test_scope_keyed_v2_tables_preserve_same_trade_id_across_accounts(tmp_path):
    chdb = pytest.importorskip("chdb")
    from chdb.session import Session

    session = Session(str(tmp_path / "scoped-v2-chdb"))
    try:
        session.query(_ddl("trade_runtime_state_v2", runtime=True))
        session.query(_ddl("paper_closed_trade_ledger_v2", runtime=False))

        runtime_rows = [
            {
                "account_scope": scope,
                "trade_id": "same-trade-id",
                "symbol": "BTCUSDT",
                "state": "position_opened",
                "payload_json": json.dumps(
                    {
                        "trade_id": "same-trade-id",
                        "account_scope": scope,
                        "symbol": "BTCUSDT",
                        "state": "position_opened",
                    }
                ),
                "legacy_payload_json": '{"trade_id":"same-trade-id"}',
                "updated_at": "2026-11-23 12:00:00.000",
                "source_fingerprint": fingerprint,
                "source_occurrence": 0,
                "payload_sha256": payload_hash,
            }
            for scope, fingerprint, payload_hash in (
                ("paper:account-a", "a" * 64, "c" * 64),
                ("paper:account-b", "b" * 64, "d" * 64),
            )
        ]
        for row in runtime_rows:
            session.query(
                "INSERT INTO trade_runtime_state_v2 FORMAT JSONEachRow\n"
                + json.dumps(row)
            )

        closed_rows = [
            {
                "account_scope": scope,
                "trade_id": "same-trade-id",
                "payload_json": json.dumps(
                    {
                        "trade_id": "same-trade-id",
                        "account_scope": scope,
                        "state": "position_closed",
                        "pnl": 1.0,
                    }
                ),
                "legacy_payload_json": '{"trade_id":"same-trade-id"}',
                "updated_at": "2026-11-23 12:00:00.000",
                "source_fingerprint": fingerprint,
                "source_occurrence": 0,
                "payload_sha256": payload_hash,
            }
            for scope, fingerprint, payload_hash in (
                ("paper:account-a", "e" * 64, "1" * 64),
                ("paper:account-b", "f" * 64, "2" * 64),
            )
        ]
        for row in closed_rows:
            session.query(
                "INSERT INTO paper_closed_trade_ledger_v2 FORMAT JSONEachRow\n"
                + json.dumps(row)
            )

        runtime = json.loads(
            str(
                session.query(
                    "SELECT account_scope, trade_id, count() "
                    "FROM trade_runtime_state_v2 "
                    "GROUP BY account_scope, trade_id ORDER BY account_scope",
                    "JSONCompact",
                )
            )
        )["data"]
        closed = json.loads(
            str(
                session.query(
                    "SELECT account_scope, trade_id, count() "
                    "FROM paper_closed_trade_ledger_v2 "
                    "GROUP BY account_scope, trade_id ORDER BY account_scope",
                    "JSONCompact",
                )
            )
        )["data"]

        assert runtime == [
            ["paper:account-a", "same-trade-id", 1],
            ["paper:account-b", "same-trade-id", 1],
        ]
        assert closed == runtime
    finally:
        session.close()
