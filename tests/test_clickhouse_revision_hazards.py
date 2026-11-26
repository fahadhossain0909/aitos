"""Executable counterexamples, NOT fixes or production-readiness assertions.

All writes/forced merges are confined to pytest tmp_path embedded ClickHouse.
These passing tests document remaining hazards in the CURRENT legacy schema.
Run explicitly with -m integration; no external service or exchange is used.
"""

import json
from types import SimpleNamespace

import pytest

from aitos.trading.persistent_state import (
    CREATE_EXCHANGE_INCOME_CURSOR,
    CREATE_PAPER_CLOSED_TRADE_LEDGER,
    CREATE_RUNTIME_STATE,
    DurableTradingStateStore,
)

pytestmark = pytest.mark.integration
TABLES = {
    "trade_runtime_state",
    "paper_closed_trade_ledger",
    "exchange_income_sync_cursor",
}


@pytest.fixture
def engine(tmp_path):
    pytest.importorskip("chdb")
    from chdb.session import Session

    session = Session(str(tmp_path / "revision-hazards-only"))
    for ddl in [
        CREATE_RUNTIME_STATE,
        CREATE_PAPER_CLOSED_TRADE_LEDGER,
        CREATE_EXCHANGE_INCOME_CURSOR,
    ]:
        session.query(ddl)
    for table in TABLES:
        session.query(f"SYSTEM STOP MERGES {table}")
    try:
        yield session
    finally:
        session.close()


def rows(engine, sql, parameters=None):
    return json.loads(str(engine.query(sql, "JSONCompact", params=parameters)))["data"]


class Client:
    def __init__(self, engine):
        self.engine = engine

    async def query(self, sql, parameters=None):
        return SimpleNamespace(result_rows=rows(self.engine, sql, parameters))


def store(engine, account_scope=""):
    return DurableTradingStateStore(
        SimpleNamespace(_client=Client(engine)), account_scope=account_scope
    )


def payload(stamp, *, state="position_opened", quantity=1, pnl=0, symbol="BTCUSDT"):
    return {
        "trade_id": "synthetic-shared-id",
        "symbol": symbol,
        "side": "LONG",
        "state": state,
        "pnl": pnl,
        "entry_price": 100,
        "quantity": quantity,
        "leverage": 1,
        "position_size_usd": 100 * quantity,
        "risk_amount_usd": 2,
        "sl_price": 98,
        "tp_price": 110,
        "entry_time": "2026-11-01T00:00:00Z",
        "exit_time": "2026-11-02T00:00:00Z" if state == "position_closed" else None,
        "updated_at": stamp.replace(" ", "T") + "Z",
    }


def insert(engine, table, item, stamp):
    assert table in TABLES
    record = {
        "trade_id": item["trade_id"],
        "payload_json": json.dumps(item),
        "updated_at": stamp,
    }
    if table == "trade_runtime_state":
        record.update(symbol=item["symbol"], state=item["state"])
    engine.query(f"INSERT INTO {table} FORMAT JSONEachRow\n" + json.dumps(record))


@pytest.mark.parametrize("table", ["trade_runtime_state", "paper_closed_trade_ledger"])
async def test_counterexample_merge_erases_conflict_evidence(engine, table):
    stamp = "2026-11-02 12:00:00.001"
    state = "position_opened" if table == "trade_runtime_state" else "position_closed"
    for pnl in [1, 7]:
        insert(engine, table, payload(stamp, state=state, pnl=pnl), stamp)
    assert rows(engine, f"SELECT count(), uniqExact(payload_json) FROM {table}") == [
        [2, 2]
    ]
    actual = store(engine)
    read = (
        actual.load_open_trades
        if state == "position_opened"
        else lambda: actual.query_closed_history(limit=10)
    )
    with pytest.raises((RuntimeError, ValueError), match="unambiguous|ambiguous"):
        await read()

    # These commands mutate ONLY this test's temporary embedded tables.
    engine.query(f"SYSTEM START MERGES {table}")
    engine.query(f"OPTIMIZE TABLE {table} FINAL")
    assert rows(engine, f"SELECT count(), uniqExact(payload_json) FROM {table}") == [
        [1, 1]
    ]
    accepted = await read()
    assert len(accepted) == 1  # The earlier conflict can no longer be detected.
    pnl = accepted[0].pnl if state == "position_opened" else accepted[0]["pnl"]
    assert pnl in {1, 7}  # Do NOT assert an authoritative winner for a timestamp tie.


async def test_counterexample_wall_clock_rollback_loses_newer_reduction(engine):
    old = "2026-11-02 12:00:00.002"
    new = "2026-11-02 12:00:00.001"
    insert(engine, "trade_runtime_state", payload(old, quantity=1), old)
    # Logically later partial reduction, after a backwards wall-clock step.
    insert(engine, "trade_runtime_state", payload(new, quantity=0.5), new)
    restored = await store(engine).load_open_trades()
    assert len(restored) == 1 and restored[0].quantity == 1
    assert rows(engine, "SELECT count() FROM trade_runtime_state") == [[2]]


async def test_counterexample_stale_unfenced_writer_with_ahead_clock_wins(engine):
    current = "2026-11-02 12:00:00.001"
    stale = "2026-11-02 12:00:01.000"
    insert(engine, "trade_runtime_state", payload(current, quantity=0.5), current)
    # Simulated old process with an ahead clock: no epoch/CAS is checked.
    insert(engine, "trade_runtime_state", payload(stale, quantity=1), stale)
    restored = await store(engine).load_open_trades()
    assert len(restored) == 1 and restored[0].quantity == 1


async def test_counterexample_submillisecond_updates_collapse_to_one_version(engine):
    for stamp, quantity in [
        ("2026-11-02 12:00:00.001100", 1),
        ("2026-11-02 12:00:00.001900", 0.5),
    ]:
        insert(engine, "trade_runtime_state", payload(stamp, quantity=quantity), stamp)
    assert rows(
        engine,
        "SELECT count(), uniqExact(updated_at), uniqExact(payload_json) FROM trade_runtime_state",
    ) == [[2, 1, 2]]
    with pytest.raises(RuntimeError, match="unambiguous"):
        await store(engine).load_open_trades()


async def test_account_scope_recovery_isolates_reused_trade_ids(engine):
    for account, symbol, stamp in [
        ("synthetic-paper-a", "BTCUSDT", "2026-11-02 12:00:00.001"),
        ("synthetic-paper-b", "ETHUSDT", "2026-11-02 12:00:00.002"),
    ]:
        item = payload(stamp, symbol=symbol)
        item["account_scope"] = account
        insert(engine, "trade_runtime_state", item, stamp)

    restored_a = await store(
        engine, account_scope="synthetic-paper-a"
    ).load_open_trades()
    restored_b = await store(
        engine, account_scope="synthetic-paper-b"
    ).load_open_trades()
    assert [(trade.symbol, trade.account_scope) for trade in restored_a] == [
        ("BTCUSDT", "synthetic-paper-a")
    ]
    assert [(trade.symbol, trade.account_scope) for trade in restored_b] == [
        ("ETHUSDT", "synthetic-paper-b")
    ]


async def test_legacy_replacing_key_still_collapses_reused_ids_after_merge(engine):
    for account, symbol, stamp in [
        ("synthetic-paper-a", "BTCUSDT", "2026-11-02 12:00:00.001"),
        ("synthetic-paper-b", "ETHUSDT", "2026-11-02 12:00:00.002"),
    ]:
        item = payload(stamp, symbol=symbol)
        item["account_scope"] = account
        insert(engine, "trade_runtime_state", item, stamp)

    # The reader is scope-aware, but the legacy ReplacingMergeTree ORDER BY
    # trade_id is not. A merge can still erase one account's distinct trade.
    engine.query("SYSTEM START MERGES trade_runtime_state")
    engine.query("OPTIMIZE TABLE trade_runtime_state FINAL")
    assert rows(engine, "SELECT count() FROM trade_runtime_state") == [[1]]
    winner = rows(
        engine,
        "SELECT DISTINCT JSONExtractString(payload_json, 'account_scope') "
        "FROM trade_runtime_state",
    )[0][0]
    assert len(await store(engine, account_scope=winner).load_open_trades()) == 1


async def test_closed_trade_in_one_scope_does_not_suppress_other_scope_open(engine):
    stamp = "2026-11-02 12:00:00.001"
    closed = payload(stamp, state="position_closed")
    closed["account_scope"] = "synthetic-paper-a"
    opened = payload(stamp, symbol="ETHUSDT")
    opened["account_scope"] = "synthetic-paper-b"
    insert(engine, "paper_closed_trade_ledger", closed, stamp)
    insert(engine, "trade_runtime_state", opened, stamp)
    restored = await store(engine, account_scope="synthetic-paper-b").load_open_trades()
    assert len(restored) == 1
    assert restored[0].symbol == "ETHUSDT"
    assert rows(engine, "SELECT count() FROM trade_runtime_state") == [[1]]


async def test_income_cursor_is_monotonic_high_water_mark(
    engine,
):
    for cursor, stamp in [
        (200, "2026-11-02 12:00:00.001"),
        (100, "2026-11-02 12:00:00.002"),
    ]:
        record = {
            "account": "synthetic-account",
            "cursor_time_ms": cursor,
            "updated_at": stamp,
        }
        engine.query(
            "INSERT INTO exchange_income_sync_cursor FORMAT JSONEachRow\n"
            + json.dumps(record)
        )
    assert await store(engine).load_income_cursor("synthetic-account") == 200
