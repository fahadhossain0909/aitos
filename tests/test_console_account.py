from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_api import register_console_routes
from aitos.console_gateway import READ_QUERIES
from aitos.exchange.operator_account import BinanceOperatorAccount

TOKEN = "synthetic-account-token-" + "x" * 32


async def test_attached_account_requires_auth_even_without_modules():
    with pytest.raises(ValueError):
        register_console_routes(web.Application(), account_observer=object())


async def test_observation_route_auth_no_queries_and_no_writes():
    calls = []

    async def observe():
        calls.append(True)
        return {
            "schema_version": 1,
            "controls": "read_only",
            "totals": {"balance": "1.00000001"},
        }

    app = web.Application()
    register_console_routes(
        app, account_observer=SimpleNamespace(observe=observe), token=TOKEN
    )
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/account")).status == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert (
            await client.get("/console/v1/account?account=other", headers=headers)
        ).status == 400
        assert (await client.post("/console/v1/account", headers=headers)).status == 405
        assert not calls
        response = await client.get("/console/v1/account", headers=headers)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["totals"]["balance"] == "1.00000001"
        assert calls == [True]
    assert READ_QUERIES["/console/v1/account"] == frozenset()


@pytest.mark.parametrize("attached", [True, False])
async def test_unavailable_never_returns_sample_or_provider_exception(attached):
    async def observe():
        raise RuntimeError("sensitive-provider-credential")

    app = web.Application()
    register_console_routes(
        app,
        account_observer=SimpleNamespace(observe=observe) if attached else None,
        token=TOKEN,
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/console/v1/account", headers={"Authorization": "Bearer " + TOKEN}
        )
        assert response.status == 503
        body = await response.json()
        assert body["available"] is False
        assert "totals" not in body and "sensitive" not in str(body)


async def test_real_adapter_to_authenticated_route_uses_only_get_paths():
    calls = []

    async def get(path, params):
        calls.append(path)
        if path.endswith("account"):
            return dict(
                totalWalletBalance="10",
                totalUnrealizedProfit="1",
                totalMarginBalance="11",
                totalInitialMargin="2",
                totalMaintMargin="1",
                availableBalance="8",
            )
        return []

    app = web.Application()
    register_console_routes(
        app, account_observer=BinanceOperatorAccount(get), token=TOKEN
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/console/v1/account", headers={"Authorization": "Bearer " + TOKEN}
        )
        assert response.status == 200
        assert (await response.json())["totals"]["totalMarginBalance"] == "11"
    assert set(calls) == {"/fapi/v3/account", "/fapi/v3/positionRisk"}


async def test_account_availability_is_explicit_not_runtime_authority():
    app = web.Application()
    register_console_routes(
        app, token=TOKEN, account_observer=SimpleNamespace(timeline=object())
    )
    async with TestClient(TestServer(app)) as client:
        result = await client.get(
            "/console/v1/capabilities", headers={"Authorization": "Bearer " + TOKEN}
        )
        body = await result.json()
        caps = {row["id"]: row["available"] for row in body["capabilities"]}
        assert (
            caps["account.margin"]
            and caps["account.equity_timeline"]
            and caps["market.mark_price"]
        )
        assert not caps["orders.protection"] and not caps["alerts.delivery"]
        assert not caps["ledger.consistent_cursor"] and not caps["runtime.control"]
        assert body["live_authorized"] is body["production_certified"] is False
