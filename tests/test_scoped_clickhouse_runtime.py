"""Scope-keyed ClickHouse runtime cutover contracts."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitos.models.trade import TradeLifecycleState
from aitos.trading.persistent_state import (
    DurableTradingStateStore,
    _scoped_state_fingerprint,
    _scoped_state_payload_hash,
)
from scripts.migrate_scoped_clickhouse_state import (
    _scoped_payload_hash,
    source_fingerprint,
)


class FakeClickHouse:
    def __init__(self, *, scoped=True):
        self.scoped = scoped
        self.commands = []
        self.inserts = []
        self.queries = []

    async def command(self, query, **kwargs):
        self.commands.append((query, kwargs))

    async def query(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "FROM system.columns" in query:
            scope_columns = [
                ("trade_runtime_state", "account_scope"),
                ("paper_closed_trade_ledger", "account_scope"),
            ]
            if self.scoped:
                return SimpleNamespace(result_rows=scope_columns)
            return SimpleNamespace(
                result_rows=[
                    ("trade_runtime_state", "trade_id"),
                    ("paper_closed_trade_ledger", "trade_id"),
                ]
            )
        if "FROM system.tables" in query:
            return SimpleNamespace(
                result_rows=[
                    ("trade_runtime_state", "MergeTree", "account_scope, trade_id"),
                    ("paper_closed_trade_ledger", "MergeTree", "account_scope, trade_id"),
                ]
            )
        if "SELECT count() FROM" in query:
            return SimpleNamespace(result_rows=[(0,)])
        return SimpleNamespace(result_rows=[])

    async def insert(self, table, rows, column_names=None):
        self.inserts.append((table, rows, column_names))


@pytest.mark.asyncio
async def test_live_state_store_requires_scope_keyed_runtime_and_closed_tables():
    client = FakeClickHouse(scoped=True)
    store = DurableTradingStateStore(
        SimpleNamespace(_client=client),
        account_scope="live:account",
        require_scoped_schema=True,
    )

    await store.initialize()

    assert store._scoped_clickhouse_state is True
    assert not any(
        "CREATE TABLE IF NOT EXISTS trade_runtime_state (" in query
        for query, _ in client.commands
    )


@pytest.mark.asyncio
async def test_live_state_store_fails_closed_on_legacy_clickhouse_schema():
    client = FakeClickHouse(scoped=False)
    store = DurableTradingStateStore(
        SimpleNamespace(_client=client),
        account_scope="live:account",
        require_scoped_schema=True,
    )

    with pytest.raises(RuntimeError, match="scope-keyed ClickHouse"):
        await store.initialize()

    assert not any(
        "CREATE TABLE IF NOT EXISTS trade_runtime_state (" in query
        for query, _ in client.commands
    )


@pytest.mark.asyncio
async def test_scoped_runtime_write_includes_scope_fingerprint_and_payload_hash():
    client = FakeClickHouse()
    store = DurableTradingStateStore(
        SimpleNamespace(_client=client),
        account_scope="paper:one",
        require_scoped_schema=True,
    )
    store._scoped_clickhouse_state = True
    updated_at = "2026-11-26T10:00:00.123+00:00"
    trade = SimpleNamespace(
        trade_id="trade-1",
        symbol="BTCUSDT",
        state=SimpleNamespace(value="position_opened"),
        updated_at=updated_at,
        account_scope="paper:one",
        to_dict=lambda: {
            "trade_id": "trade-1",
            "symbol": "BTCUSDT",
            "state": "position_opened",
            "updated_at": updated_at,
        },
    )

    await store.save_trade(trade)

    table, rows, columns = client.inserts[-1]
    assert table == "trade_runtime_state"
    assert columns == [
        "account_scope",
        "trade_id",
        "symbol",
        "state",
        "payload_json",
        "legacy_payload_json",
        "updated_at",
        "source_fingerprint",
        "source_occurrence",
        "payload_sha256",
    ]
    row = rows[0]
    assert row[0:4] == ["paper:one", "trade-1", "BTCUSDT", "position_opened"]
    assert row[4] == row[5]
    assert len(row[7]) == 64
    assert row[8] == 0
    assert len(row[9]) == 64


@pytest.mark.asyncio
async def test_scoped_delete_appends_tombstone_instead_of_mutating_history():
    client = FakeClickHouse()
    store = DurableTradingStateStore(
        SimpleNamespace(_client=client),
        account_scope="paper:one",
        require_scoped_schema=True,
    )
    store._scoped_clickhouse_state = True

    await store.delete_trade("trade-1")

    table, rows, columns = client.inserts[-1]
    assert table == "trade_runtime_state"
    payload = __import__("json").loads(rows[0][4])
    assert payload == {
        "account_scope": "paper:one",
        "state": "position_deleted",
        "symbol": "",
        "trade_id": "trade-1",
    }
    assert "source_occurrence" in columns
    assert not any("ALTER TABLE trade_runtime_state DELETE" in q for q, _ in client.commands)


@pytest.mark.asyncio
async def test_scoped_closed_trade_write_uses_scope_keyed_evidence_columns():
    client = FakeClickHouse()
    store = DurableTradingStateStore(
        SimpleNamespace(_client=client),
        account_scope="paper:one",
        require_scoped_schema=True,
    )
    store._scoped_clickhouse_state = True
    trade = SimpleNamespace(
        trade_id="trade-closed",
        symbol="BTCUSDT",
        state=TradeLifecycleState.POSITION_CLOSED,
        account_scope="paper:one",
        updated_at=datetime.now(timezone.utc).isoformat(),
        to_dict=lambda: {
            "trade_id": "trade-closed",
            "symbol": "BTCUSDT",
            "state": "position_closed",
            "account_scope": "paper:one",
            "pnl": 1.0,
        },
    )

    await store.save_closed_trade(trade)

    table, rows, columns = client.inserts[-1]
    assert table == "paper_closed_trade_ledger"
    assert columns == [
        "account_scope",
        "trade_id",
        "payload_json",
        "legacy_payload_json",
        "updated_at",
        "source_fingerprint",
        "source_occurrence",
        "payload_sha256",
    ]
    assert rows[0][0:2] == ["paper:one", "trade-closed"]
    assert rows[0][2] == rows[0][3]
    assert len(rows[0][5]) == 64
    assert len(rows[0][7]) == 64



def test_runtime_row_identity_matches_scoped_migration_fingerprints():
    payload = {
        "trade_id": "trade-identity",
        "symbol": "BTCUSDT",
        "state": "position_opened",
        "account_scope": "paper:one",
    }
    raw = __import__("json").dumps(
        payload, sort_keys=True, default=str, allow_nan=False
    )
    updated_at = datetime(2026, 11, 26, 10, 0, 0, 123000, tzinfo=timezone.utc)

    assert _scoped_state_fingerprint(
        "trade_runtime_state", "trade-identity", raw, updated_at
    ) == source_fingerprint(
        "trade_runtime_state", "trade-identity", raw, updated_at
    )
    assert _scoped_state_payload_hash(payload, "paper:one") == _scoped_payload_hash(
        payload, "paper:one"
    )
