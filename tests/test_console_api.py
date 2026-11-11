"""Offline read-only console contracts; no external services or orders."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aitos.console_api import register_console_routes, finite_json
from aitos.trading.persistent_state import DurableTradingStateStore

TOKEN = "test-only-console-token-" + "x" * 32


async def client_for(**kwargs):
    app = web.Application()
    register_console_routes(app, **kwargs)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_unattached_snapshot_never_fabricates_financial_data():
    async with await client_for() as client:
        response = await client.get("/console/v1/snapshot")
        body = await response.json()
        assert response.headers["Cache-Control"] == "no-store"
        assert not body["connected"]
        assert body["positions"] == body["pipeline"] == []
        assert body["controls"] == "read_only"
        assert (await client.post("/console/v1/snapshot")).status == 405
        assert (await client.get("/console/v1/history")).status == 503


@pytest.mark.parametrize("token", [None, "", "short"])
def test_attached_console_requires_long_token(token):
    with pytest.raises(ValueError):
        register_console_routes(web.Application(), state_store=object(), token=token)


@pytest.mark.asyncio
async def test_auth_history_whitelist_and_parameter_forwarding():
    store = SimpleNamespace(
        query_closed_history=AsyncMock(
            return_value=[
                {
                    "trade_id": "one",
                    "symbol": "BTCUSDT",
                    "pnl": float("nan"),
                    "api_secret": "never expose",
                }
            ]
        )
    )
    async with await client_for(state_store=store, token=TOKEN) as client:
        assert (await client.get("/console/v1/snapshot")).status == 401
        assert (await client.get("/console/v1/history")).status == 401
        response = await client.get(
            "/console/v1/history?limit=12&offset=4&symbol=btcusdt",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        data = await response.json()
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert data["rows"][0]["pnl"] is None
        assert "api_secret" not in data["rows"][0]
        store.query_closed_history.assert_awaited_once_with(
            limit=12, offset=4, symbol="BTCUSDT"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "offset=-1",
        "offset=100001",
        "symbol=BTC%27",
        "limit=NaN",
    ],
)
async def test_history_rejects_invalid_bounds(query):
    async with await client_for() as client:
        assert (await client.get("/console/v1/history?" + query)).status == 400


@pytest.mark.asyncio
async def test_history_errors_are_sanitized():
    store = SimpleNamespace(
        query_closed_history=AsyncMock(side_effect=RuntimeError("sensitive-secret"))
    )
    async with await client_for(state_store=store, token=TOKEN) as client:
        response = await client.get(
            "/console/v1/history", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status == 503
        assert "sensitive-secret" not in await response.text()


@pytest.mark.asyncio
async def test_attached_snapshot_uses_registry_and_whitelisted_positions():
    module = SimpleNamespace(
        module_id="trade-lifecycle",
        health_check=AsyncMock(
            return_value=SimpleNamespace(status=SimpleNamespace(value="healthy"))
        ),
        get_open_trades=lambda: [
            {"trade_id": "one", "symbol": "BTCUSDT", "secret": "hidden"}
        ],
    )
    async with await client_for(modules=[module], token=TOKEN) as client:
        response = await client.get(
            "/console/v1/snapshot", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        body = await response.json()
        assert body["connected"] and body["pipeline"]
        assert body["positions"][0]["trade_id"] == "one"
        assert "secret" not in body["positions"][0]
        assert all(
            s["latency_p95_ms"] is None or s["latency_sample_count"] > 0
            for s in body["pipeline"]
        )


@pytest.mark.asyncio
async def test_history_query_is_parameterized():
    payload = {"trade_id": "one", "state": "position_closed", "pnl": 0}
    query = AsyncMock(
        return_value=SimpleNamespace(result_rows=[("one", json.dumps(payload), 1)])
    )
    store = DurableTradingStateStore(
        SimpleNamespace(_client=SimpleNamespace(query=query))
    )
    assert await store.query_closed_history(limit=10, offset=2, symbol="BTC'OR1=1") == [
        payload
    ]
    sql = query.call_args.args[0]
    assert "BTC'OR1=1" not in sql
    assert query.call_args.kwargs["parameters"] == {
        "limit": 10,
        "offset": 2,
        "symbol": "BTC'OR1=1",
    }
    with pytest.raises(ValueError):
        await store.query_closed_history(limit=101)


def test_finite_json_recursive():
    assert finite_json({"a": [float("inf"), float("-inf"), 2]}) == {
        "a": [None, None, 2]
    }


@pytest.mark.asyncio
async def test_explicit_runtime_mode_is_not_inferred_from_default_config():
    async with await client_for(runtime_mode="live") as client:
        body = await (await client.get("/console/v1/snapshot")).json()
        assert body["mode"] == "live"
        assert body["readiness"]["live_authorized"] is False
    async with await client_for() as client:
        body = await (await client.get("/console/v1/snapshot")).json()
        assert body["mode"] == "unknown"


@pytest.mark.asyncio
async def test_nonascii_wrong_authorization_is_unauthorized_not_server_error():
    async with await client_for(token=TOKEN) as client:
        assert (
            await client.get(
                "/console/v1/snapshot", headers={"Authorization": "Bearer wrong-é"}
            )
        ).status == 401


@pytest.mark.asyncio
async def test_performance_endpoint_is_authenticated_fixed_bound_and_read_only():
    store = SimpleNamespace(
        query_closed_performance=AsyncMock(
            return_value={
                "schema_version": 1,
                "available": True,
                "source": "clickhouse_closed_ledger",
            }
        )
    )
    async with await client_for(state_store=store, token=TOKEN) as client:
        assert (await client.get("/console/v1/performance")).status == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        response = await client.get("/console/v1/performance", headers=headers)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "generated_at" in await response.json()
        store.query_closed_performance.assert_awaited_once_with(limit=1000)
        assert (
            await client.get("/console/v1/performance?limit=100000", headers=headers)
        ).status == 400
        assert (
            await client.post("/console/v1/performance", headers=headers)
        ).status == 405


@pytest.mark.asyncio
async def test_performance_errors_never_return_sensitive_store_errors():
    store = SimpleNamespace(
        query_closed_performance=AsyncMock(side_effect=RuntimeError("sensitive-secret"))
    )
    async with await client_for(state_store=store, token=TOKEN) as client:
        response = await client.get(
            "/console/v1/performance", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert (
            response.status == 503 and "sensitive-secret" not in await response.text()
        )
    async with await client_for() as client:
        assert (await client.get("/console/v1/performance")).status == 503


@pytest.mark.asyncio
async def test_research_plan_is_read_only_computation_not_job_launch():
    async with await client_for() as client:
        response = await client.get(
            "/console/v1/research/plan?start=2025-01-01&end=2026-11-07"
        )
        report = await response.json()
        assert response.status == 200 and report["fold_count"] > 0
        assert report["backtest_executed"] is report["promotion_authorized"] is False
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.post("/console/v1/research/plan")).status == 405


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "",
        "start=bad&end=2026-11-07",
        "start=2025-01-01&end=2026-11-07&test_days=0",
        "start=2025-01-01&end=2026-11-07&unknown=1",
        "start=2025-01-01&start=2024-01-01&end=2026-11-07",
    ],
)
async def test_research_plan_rejects_invalid_or_duplicate_inputs(query):
    async with await client_for() as client:
        assert (await client.get("/console/v1/research/plan?" + query)).status == 400
