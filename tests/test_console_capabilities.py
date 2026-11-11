from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_api import register_console_routes
from aitos.console_capabilities import RuntimeEndpoint
from aitos.native.console_host import LocalConsoleHost


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com/api",
        "https://example.com?token=secret",
        "file:///tmp/api",
        "https://example.com/#token",
    ],
)
def test_unsafe_or_ambiguous_endpoints_rejected(origin):
    with pytest.raises(ValueError):
        RuntimeEndpoint(origin)


def test_explicit_origin_and_secret_scope_are_bound():
    assert RuntimeEndpoint("https://EXAMPLE.com:443/").origin == "https://example.com"
    assert (
        RuntimeEndpoint("https://example.com/").token_slot
        == RuntimeEndpoint("https://EXAMPLE.com:443").token_slot
    )
    assert (
        RuntimeEndpoint("https://example.com").token_slot
        != RuntimeEndpoint("https://other.example.com").token_slot
    )
    assert RuntimeEndpoint("http://127.0.0.1:18090").origin.endswith(":18090")


def test_native_host_accepts_remote_https_without_starting_any_service(tmp_path):
    host = LocalConsoleHost(
        tmp_path,
        upstream_origin="https://runtime.example",
        password="x" * 24,
        token="x" * 32,
    )
    assert host._endpoint.origin == "https://runtime.example"
    assert host._thread is None
    with pytest.raises(ValueError):
        LocalConsoleHost(
            tmp_path,
            upstream_origin="https://runtime.example",
            upstream_port=8090,
            password="x" * 24,
            token="x" * 32,
        )


async def test_capability_auth_availability_and_no_mutation_authority():
    token = "synthetic-token-" + "x" * 32
    app = web.Application()
    store = SimpleNamespace(query_closed_history=lambda: None)
    register_console_routes(app, state_store=store, token=token)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/capabilities")).status == 401
        response = await client.get(
            "/console/v1/capabilities", headers={"Authorization": "Bearer " + token}
        )
        body = await response.json()
        assert response.headers["Cache-Control"] == "no-store"
        assert body["api_version"] == "1.0" and body["controls"] == "read_only"
        caps = {c["id"]: c["available"] for c in body["capabilities"]}
        assert caps["ledger.history"] and not caps["ledger.performance_window"]
        for name in (
            "account.equity_timeline",
            "account.margin",
            "market.mark_price",
            "orders.protection",
            "alerts.delivery",
            "configuration.approval",
            "runtime.control",
        ):
            assert caps[name] is False
        assert body["live_authorized"] is body["production_certified"] is False
        assert (await client.post("/console/v1/capabilities")).status == 405
        assert (
            await client.get(
                "/console/v1/capabilities?url=https://other.example",
                headers={"Authorization": "Bearer " + token},
            )
        ).status == 400
