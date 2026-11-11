"""Optional REAL ClickHouse-engine SQL checks via chDB, no network or production DB.

Run: pytest tests/test_clickhouse_embedded_ledger.py -m integration
Install the test-only chdb extra separately. This is not a server/transport test.
"""

import json
from types import SimpleNamespace

import pytest

from aitos.trading.persistent_state import (
    CREATE_PAPER_CLOSED_TRADE_LEDGER,
    CREATE_RUNTIME_STATE,
    DurableTradingStateStore,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(tmp_path):
    pytest.importorskip("chdb")
    from chdb.session import Session

    session = Session(str(tmp_path / "isolated-chdb"))
    session.query(CREATE_RUNTIME_STATE)
    session.query(CREATE_PAPER_CLOSED_TRADE_LEDGER)
    # Retain timestamp collisions so the reader can prove conflict detection.
    session.query("SYSTEM STOP MERGES trade_runtime_state")
    session.query("SYSTEM STOP MERGES paper_closed_trade_ledger")
    yield session
    session.close()


class Client:
    def __init__(self, session):
        self.session = session

    async def query(self, sql, parameters=None):
        data = json.loads(
            str(self.session.query(sql, "JSONCompact", params=parameters))
        )
        return SimpleNamespace(result_rows=data["data"])


def store(engine):
    return DurableTradingStateStore(SimpleNamespace(_client=Client(engine)))


def trade(i="one", state="position_closed", pnl=5):
    return dict(
        trade_id=i,
        symbol="BTCUSDT",
        side="LONG",
        state=state,
        pnl=pnl,
        entry_price=100,
        quantity=1,
        leverage=1,
        position_size_usd=100,
        risk_amount_usd=2,
        sl_price=98,
        tp_price=110,
        entry_time="2026-11-01T00:00:00Z",
        exit_time="2026-11-02T00:00:00Z",
    )


def insert(engine, table, payload, stamp):
    if table == "trade_runtime_state":
        record = dict(
            trade_id=payload["trade_id"],
            symbol=payload["symbol"],
            state=payload["state"],
            payload_json=json.dumps(payload),
            updated_at=stamp,
        )
    else:
        record = dict(
            trade_id=payload["trade_id"],
            payload_json=json.dumps(payload),
            updated_at=stamp,
        )
    # JSONEachRow avoids constructing SQL literals from payload strings.
    engine.query(f"INSERT INTO {table} FORMAT JSONEachRow\n" + json.dumps(record))


async def test_engine_latest_closed_and_parameterized_history_performance(engine):
    insert(engine, "paper_closed_trade_ledger", trade(pnl=1), "2026-11-02 12:00:00.000")
    insert(engine, "paper_closed_trade_ledger", trade(pnl=7), "2026-11-02 12:00:00.001")
    actual = store(engine)
    rows = await actual.query_closed_history(limit=10, symbol="BTCUSDT")
    assert len(rows) == 1 and rows[0]["pnl"] == 7
    assert await actual.query_closed_history(limit=10, symbol="BTC' OR 1=1") == []
    assert (await actual.query_closed_performance())["summary"]["net_pnl_usd"] == 7
    assert len(await actual.load_closed_trades()) == 1


async def test_engine_timestamp_tied_closed_payloads_fail_closed(engine):
    for pnl in [1, 7]:
        insert(
            engine,
            "paper_closed_trade_ledger",
            trade(pnl=pnl),
            "2026-11-02 12:00:00.001",
        )
    with pytest.raises(ValueError, match="ambiguous"):
        await store(engine).query_closed_history(limit=10)
    with pytest.raises(RuntimeError, match="unambiguous"):
        await store(engine).load_closed_trades()


async def test_engine_later_terminal_runtime_record_not_resurrected(engine):
    insert(
        engine,
        "trade_runtime_state",
        trade(state="position_opened"),
        "2026-11-02 12:00:00.000",
    )
    insert(engine, "trade_runtime_state", trade(), "2026-11-02 12:00:00.001")
    assert await store(engine).load_open_trades() == []


async def test_engine_open_timestamp_conflict_blocks_instead_of_selecting_random(
    engine,
):
    for pnl in [1, 7]:
        insert(
            engine,
            "trade_runtime_state",
            trade(state="position_opened", pnl=pnl),
            "2026-11-02 12:00:00.001",
        )
    with pytest.raises(RuntimeError, match="unambiguous"):
        await store(engine).load_open_trades()


async def test_engine_identical_retries_are_not_a_payload_conflict(engine):
    for _ in range(2):
        insert(engine, "paper_closed_trade_ledger", trade(), "2026-11-02 12:00:00.001")
    assert len(await store(engine).query_closed_history(limit=10)) == 1


async def test_engine_read_budget_throws_instead_of_returning_partial_financials(
    engine, monkeypatch
):
    insert(engine, "paper_closed_trade_ledger", trade(), "2026-11-02 12:00:00.001")
    from aitos.trading import persistent_state

    monkeypatch.setattr(
        persistent_state,
        "CLOSED_PERFORMANCE_QUERY",
        persistent_state.CLOSED_PERFORMANCE_QUERY.replace(
            "max_bytes_to_read=67108864", "max_bytes_to_read=1"
        ),
    )
    with pytest.raises(Exception) as caught:
        await store(engine).query_closed_performance()
    assert "TOO_MANY_BYTES" in str(caught.value)
