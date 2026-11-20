"""Opt-in three-store recovery drill: real ClickHouse + SQLite + Binance adapter.

The fault matrix talks to a real ClickHouse server and a real SQLite journal.
The BinanceFuturesOrderExecutor uses its real aiohttp/HMAC request path against
a deterministic local venue protocol fixture so destructive orders are never
sent to an exchange. A separate opt-in test performs authenticated, read-only
Binance demo-testnet inventory checks.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

from aiohttp import web
import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


def _clickhouse_enabled() -> bool:
    return os.getenv("AITOS_ENABLE_THREE_STORE_FAULT_TESTS") == "1"


@pytest_asyncio.fixture
async def clickhouse_store():
    if not _clickhouse_enabled():
        pytest.skip("set AITOS_ENABLE_THREE_STORE_FAULT_TESTS=1 to opt in")
    pytest.importorskip("clickhouse_connect")
    import clickhouse_connect

    host = os.getenv("AITOS_FAULT_CLICKHOUSE_HOST")
    if not host:
        pytest.skip("AITOS_FAULT_CLICKHOUSE_HOST is required")
    port = int(os.getenv("AITOS_FAULT_CLICKHOUSE_PORT", "8123"))
    user = os.getenv("AITOS_FAULT_CLICKHOUSE_USER", "default")
    password = os.getenv("AITOS_FAULT_CLICKHOUSE_PASSWORD", "")
    database = "aitos_fault_" + uuid.uuid4().hex[:12]
    admin = await clickhouse_connect.get_async_client(
        host=host, port=port, username=user, password=password
    )
    try:
        await admin.command(f"CREATE DATABASE {database}")
    finally:
        await admin.close()
    client = await clickhouse_connect.get_async_client(
        host=host, port=port, username=user, password=password, database=database
    )
    from aitos.trading.persistent_state import (
        CREATE_PAPER_CLOSED_TRADE_LEDGER,
        CREATE_RUNTIME_STATE,
        DurableTradingStateStore,
    )

    await client.command(CREATE_RUNTIME_STATE)
    await client.command(CREATE_PAPER_CLOSED_TRADE_LEDGER)
    store = DurableTradingStateStore(SimpleNamespace(_client=client))
    try:
        yield store
    finally:
        await client.close()
        admin = await clickhouse_connect.get_async_client(
            host=host, port=port, username=user, password=password
        )
        try:
            await admin.command(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await admin.close()


@pytest_asyncio.fixture
async def venue_http():
    """A deterministic Binance-compatible HTTP endpoint, not an exchange."""
    secret = "integration-secret"
    algo = {
        "algoId": 123,
        "actualOrderId": 456,
        "clientAlgoId": "fault-stop-client",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "algoStatus": "TRIGGERED",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "closePosition": False,
    }
    child = {
        "orderId": 456,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "status": "PARTIALLY_FILLED",
        "origQty": "1.0",
        "executedQty": "0.25",
        "cumQuote": "22.5",
        "avgPrice": "90",
    }
    requests = []

    async def handle(request):
        assert request.method == "GET"
        assert request.headers.get("X-MBX-APIKEY") == "integration-key"
        raw = request.query_string
        assert "&signature=" in raw
        unsigned, signature = raw.rsplit("&signature=", 1)
        expected = hmac.new(
            secret.encode(), unsigned.encode(), hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(signature, expected)
        query = parse_qs(unsigned)
        requests.append((request.method, request.path, query))
        if request.path == "/fapi/v1/algoOrder":
            return web.json_response(algo)
        if request.path == "/fapi/v1/order":
            return web.json_response(child)
        return web.json_response({"code": -404, "msg": "unknown route"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        await runner.cleanup()


def _trade():
    from aitos.models.trade import Trade, TradeLifecycleState, TradeSide

    return Trade(
        trade_id="trade-three-store",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=10.0,
        strategy_id="fault-drill",
        agent_consensus={},
        explanation="isolated three-store fault drill",
        sl_price=90.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-20T00:00:00+00:00",
        sl_order_id="algo:123",
    )


def _seed_intent(journal):
    from aitos.execution.order_executor import OrderRequest, OrderResult
    from aitos.models.trade import TradeSide

    journal.begin(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=1.0,
            reference_price=90.0,
            order_type="STOP_MARKET",
            client_order_id="fault-stop-client",
            reduce_only=True,
            owner_trade_id="trade-three-store",
        )
    )
    journal.record_protection(
        "fault-stop-client",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            90.0,
            client_order_id="fault-stop-client",
        ),
    )
    journal.mark_applied("fault-stop-client", "trade-three-store")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["clickhouse_snapshot", "sqlite_watermark"])
async def test_three_store_child_fill_recovery_survives_crash_and_restart(
    failure_point,
    clickhouse_store,
    venue_http,
    event_bus,
    risk_engine,
    tmp_path,
    monkeypatch,
):
    from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
    from aitos.execution.intent_store import ExecutionIntentStore
    from aitos.execution.protection_reconciliation import (
        recover_triggered_protection_child_fills,
    )
    from aitos.trading.lifecycle import TradeLifecycle
    from aitos.trading.persistent_state import _trade_from_dict

    store = clickhouse_store
    journal = ExecutionIntentStore(
        tmp_path / "three-store.sqlite", scope="isolated-testnet-fault-drill"
    )
    _seed_intent(journal)
    await store.save_trade(_trade())

    base_url, requests = venue_http
    executor = BinanceFuturesOrderExecutor(
        "integration-key",
        "integration-secret",
        testnet=True,
        intent_store=journal,
    )
    executor._base_url = base_url
    await executor.connect()

    async def make_lifecycle(trades):
        lifecycle = TradeLifecycle(
            event_bus=event_bus,
            risk_engine=risk_engine,
            order_executor=executor,
            use_exchange_side_stops=True,
        )
        lifecycle._open_trades.update({trade.trade_id: trade for trade in trades})
        lifecycle._execution_state_writer = store.save_trade
        lifecycle._execution_position_verifier = AsyncMock()
        return lifecycle

    try:
        first_trade = (await store.load_open_trades())[0]
        first = await make_lifecycle([first_trade])

        if failure_point == "clickhouse_snapshot":
            original_save = store.save_trade
            failed = False

            async def fail_snapshot(trade):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected ClickHouse snapshot outage")
                await original_save(trade)

            first._execution_state_writer = fail_snapshot
        else:
            original_watermark = journal.mark_protection_child_applied
            failed = False

            def fail_watermark(**kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected SQLite watermark outage")
                return original_watermark(**kwargs)

            monkeypatch.setattr(journal, "mark_protection_child_applied", fail_watermark)

        first_report = await recover_triggered_protection_child_fills(
            executor, journal, first
        )
        assert len(first_report["blocked"]) == 1
        assert first_report["safe_to_resume"] is False

        # Model process death: discard all in-memory lifecycle objects and restore
        # exclusively from the durable ClickHouse snapshot and SQLite journal.
        monkeypatch.undo()
        await asyncio.sleep(0.01)
        restored = (await store.load_open_trades())[0]
        restarted = await make_lifecycle([_trade_from_dict(restored.to_dict())])
        second_report = await recover_triggered_protection_child_fills(
            executor, journal, restarted
        )

        assert second_report["blocked"] == []
        assert len(second_report["applied"]) == 1
        recovered = restarted.get_open_trades()[0]
        assert recovered.quantity == pytest.approx(0.75)
        assert recovered.gross_realized_pnl_usd == pytest.approx(-2.5)
        watermark = journal.protection_child_watermark("123", "456")
        assert watermark["cumulative_quantity"] == "0.25"
        assert watermark["cumulative_quote"] == "22.5"
        assert any(path == "/fapi/v1/algoOrder" for _, path, _ in requests)
        assert any(path == "/fapi/v1/order" for _, path, _ in requests)
        assert all(method == "GET" for method, _, _ in requests)
        assert all(
            "signature" not in str(query)
            for _, _, query in requests
        )
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_authenticated_binance_demo_testnet_read_only_preflight():
    if os.getenv("AITOS_ENABLE_BINANCE_TESTNET_READONLY") != "1":
        pytest.skip("authenticated testnet preflight is opt-in")
    key = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY")
    secret = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET")
    if not key or not secret:
        pytest.skip("dedicated Binance Futures demo-testnet credentials are required")
    from aitos.execution.binance_executor import BinanceFuturesOrderExecutor

    executor = BinanceFuturesOrderExecutor(key, secret, testnet=True)
    await executor.connect()
    try:
        positions = await executor.get_position_risk()
        algos = await executor.get_open_algo_orders()
        assert isinstance(positions, list)
        assert isinstance(algos, list)
    finally:
        await executor.close()
