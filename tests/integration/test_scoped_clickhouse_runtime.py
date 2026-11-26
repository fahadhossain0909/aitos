"""Real ClickHouse acceptance for scope-keyed runtime state projections."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import clickhouse_connect
import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.persistent_state import DurableTradingStateStore
from scripts.migrate_scoped_clickhouse_state import _ddl


class AsyncClickHouseAdapter:
    def __init__(self, client):
        self.client = client

    async def command(self, query, **kwargs):
        return await asyncio.to_thread(self.client.command, query, **kwargs)

    async def query(self, query, parameters=None):
        return await asyncio.to_thread(self.client.query, query, parameters=parameters)

    async def insert(self, table, rows, column_names=None):
        return await asyncio.to_thread(
            self.client.insert, table, rows, column_names=column_names
        )


def _trade(scope: str, symbol: str, state: TradeLifecycleState) -> Trade:
    now = datetime.now(timezone.utc).isoformat()
    closed = state == TradeLifecycleState.POSITION_CLOSED
    return Trade(
        trade_id="shared-trade-id",
        symbol=symbol,
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=1.0,
        strategy_id="scoped-runtime-integration",
        agent_consensus={},
        explanation="ClickHouse scope-key integration fixture",
        sl_price=99.0,
        tp_price=102.0,
        state=state,
        entry_time=now,
        updated_at=now,
        pnl=2.0 if closed else None,
        gross_realized_pnl_usd=2.0 if closed else 0.0,
        account_scope=scope,
    )


@pytest.mark.asyncio
async def test_scope_keyed_clickhouse_runtime_isolates_same_trade_id_across_accounts():
    host = os.getenv("AITOS_FAULT_CLICKHOUSE_HOST")
    if not host:
        pytest.skip("requires the disposable ClickHouse service from three-store CI")
    port = int(os.getenv("AITOS_FAULT_CLICKHOUSE_PORT", "8123"))
    username = os.getenv("AITOS_FAULT_CLICKHOUSE_USER", "default")
    password = os.getenv("AITOS_FAULT_CLICKHOUSE_PASSWORD", "")
    admin = clickhouse_connect.get_client(
        host=host, port=port, username=username, password=password
    )
    database = "aitos_scoped_runtime_acceptance"
    admin.command(f"CREATE DATABASE IF NOT EXISTS {database} ENGINE=Atomic")
    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=username,
        password=password,
        database=database,
    )
    try:
        for table in (
            "trade_runtime_state",
            "paper_closed_trade_ledger",
            "portfolio_drawdown_state",
            "exchange_income_sync_cursor",
            "exchange_income_sync_retry",
            "order_execution_ledger",
        ):
            client.command(f"DROP TABLE IF EXISTS {table}")
        client.command(_ddl("trade_runtime_state", runtime=True))
        client.command(_ddl("paper_closed_trade_ledger", runtime=False))

        adapter = AsyncClickHouseAdapter(client)
        scope_a = "paper:integration-account-a"
        scope_b = "paper:integration-account-b"
        store_a = DurableTradingStateStore(
            SimpleNamespace(_client=adapter),
            account_scope=scope_a,
            require_scoped_schema=True,
        )
        store_b = DurableTradingStateStore(
            SimpleNamespace(_client=adapter),
            account_scope=scope_b,
            require_scoped_schema=True,
        )
        await store_a.initialize()
        await store_b.initialize()

        await store_a.save_trade(
            _trade(scope_a, "BTCUSDT", TradeLifecycleState.POSITION_OPENED)
        )
        await store_b.save_trade(
            _trade(scope_b, "ETHUSDT", TradeLifecycleState.POSITION_OPENED)
        )

        open_a = await store_a.load_open_trades()
        open_b = await store_b.load_open_trades()
        assert [
            (trade.trade_id, trade.symbol, trade.account_scope) for trade in open_a
        ] == [("shared-trade-id", "BTCUSDT", scope_a)]
        assert [
            (trade.trade_id, trade.symbol, trade.account_scope) for trade in open_b
        ] == [("shared-trade-id", "ETHUSDT", scope_b)]

        closed_a = _trade(scope_a, "BTCUSDT", TradeLifecycleState.POSITION_CLOSED)
        await store_a.save_closed_trade(closed_a)
        await store_a.delete_trade(closed_a.trade_id)

        assert await store_a.load_open_trades() == []
        restored_closed_a = await store_a.load_closed_trades()
        assert len(restored_closed_a) == 1
        assert restored_closed_a[0].account_scope == scope_a
        assert restored_closed_a[0].trade_id == "shared-trade-id"
        assert len(await store_b.load_closed_trades()) == 0
        assert len(await store_b.load_open_trades()) == 1

        runtime_rows = client.query(
            """
            SELECT account_scope,trade_id,source_fingerprint,source_occurrence,payload_sha256
            FROM trade_runtime_state
            ORDER BY account_scope
            """
        ).result_rows
        assert len(runtime_rows) == 2
        assert {row[0] for row in runtime_rows} == {scope_a, scope_b}
        assert all(len(row[2]) == 64 and len(row[4]) == 64 for row in runtime_rows)
        assert all(int(row[3]) >= 0 for row in runtime_rows)
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()
        admin.close()
