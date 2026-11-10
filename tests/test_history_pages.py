from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.trading import history_pages as module
from aitos.console_api import register_console_routes
from aitos.trading.persistent_state import (
    DurableTradingStateStore,
    CLOSED_HISTORY_QUERY,
)


@pytest.fixture
def pages(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "prepare_durable_directory", lambda p: p)
    source = SimpleNamespace(
        query_closed_snapshot=AsyncMock(
            return_value=dict(
                rows=[
                    dict(trade_id=str(i), symbol="BTCUSDT", pnl=i, apiKey="never-copy")
                    for i in range(5)
                ],
                truncated=False,
            )
        )
    )
    clock = SimpleNamespace(now=1000.0)
    value = module.HistoryPages(
        source,
        tmp_path,
        cursor_key=b"x" * 32,
        scope="synthetic-ledger",
        clock=lambda: clock.now,
    )
    value.clock = clock
    return value


async def test_pages_remain_stable_after_source_revision_and_reopen(pages):
    first = await pages.page(limit=2)
    assert [r["trade_id"] for r in first["rows"]] == ["0", "1"]
    pages.source.query_closed_snapshot.return_value = dict(
        rows=[dict(trade_id="new")], truncated=True
    )
    second = await pages.page(limit=2, cursor=first["next_cursor"])
    reopened = module.HistoryPages(
        pages.source,
        pages.directory,
        cursor_key=b"x" * 32,
        scope="synthetic-ledger",
        clock=lambda: 1001.0,
    )
    third = await reopened.page(limit=2, cursor=second["next_cursor"])
    assert [r["trade_id"] for r in second["rows"] + third["rows"]] == ["2", "3", "4"]
    assert third["next_cursor"] is None
    assert pages.source.query_closed_snapshot.await_count == 1
    assert "never-copy" not in str(first)


async def test_tamper_page_size_expiry_and_changed_file_rejected(pages):
    first = await pages.page(limit=2)
    cursor = first["next_cursor"]
    with pytest.raises(ValueError):
        await pages.page(limit=1, cursor=cursor)
    with pytest.raises(ValueError):
        await pages.page(limit=2, cursor=cursor + "x")
    pages.clock.now += 901
    with pytest.raises(ValueError):
        await pages.page(limit=2, cursor=cursor)
    pages.clock.now = 1000.0
    path = pages.directory / (first["snapshot_id"] + ".json")
    value = json.loads(path.read_text())
    value["rows"][2]["pnl"] = 999
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        await pages.page(limit=2, cursor=cursor)


async def test_quota_and_bad_capture_fail_without_overwrite(pages):
    pages.source.query_closed_snapshot.return_value = dict(
        rows=[dict(trade_id="same")] * 2, truncated=False
    )
    with pytest.raises(RuntimeError):
        await pages.page()
    assert not list(pages.directory.glob("*.json"))
    pages.source.query_closed_snapshot.return_value = dict(rows=[], truncated=False)
    for i in range(32):
        (pages.directory / (str(i) + ".json")).write_text("{}")
    with pytest.raises(RuntimeError):
        await pages.page()
    assert len(list(pages.directory.glob("*.json"))) == 32


async def test_authenticated_route(pages):
    token = "synthetic-token-" + "x" * 32
    with pytest.raises(ValueError):
        register_console_routes(web.Application(), history_pages=pages)
    app = web.Application()
    register_console_routes(app, token=token, history_pages=pages)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/history/cursor")).status == 401
        headers = {"Authorization": "Bearer " + token}
        response = await client.get(
            "/console/v1/history/cursor?limit=2", headers=headers
        )
        assert (
            response.status == 200 and response.headers["Cache-Control"] == "no-store"
        )
        page = await response.json()
        response = await client.get(
            "/console/v1/history/cursor",
            params={"limit": 2, "cursor": page["next_cursor"]},
            headers=headers,
        )
        assert [r["trade_id"] for r in (await response.json())["rows"]] == ["2", "3"]
        for query in ("limit=0", "limit=1&limit=2", "cursor=bad", "offset=100"):
            assert (
                await client.get("/console/v1/history/cursor?" + query, headers=headers)
            ).status == 400
        assert (
            await client.post("/console/v1/history/cursor", headers=headers)
        ).status == 405


async def test_snapshot_source_uses_fixed_bound_sql_and_checks_extra_row():
    store = object.__new__(DurableTradingStateStore)
    client = SimpleNamespace(
        query=AsyncMock(return_value=SimpleNamespace(result_rows=[]))
    )
    store._client = lambda: client
    assert await store.query_closed_snapshot(limit=10) == dict(rows=[], truncated=False)
    client.query.assert_awaited_once_with(
        CLOSED_HISTORY_QUERY, parameters={"symbol": "", "limit": 11, "offset": 0}
    )
    with pytest.raises(ValueError):
        await store.query_closed_snapshot(limit=True)


async def test_first_page_replay_stays_on_same_materialization(pages):
    first = await pages.page(limit=2)
    pages.source.query_closed_snapshot.return_value = dict(rows=[], truncated=False)
    replay = await pages.page(limit=2, cursor=first["page_cursor"])
    assert replay == first
    assert pages.source.query_closed_snapshot.await_count == 1


async def test_cross_scope_continuation_rejected(pages):
    page = await pages.page(limit=2)
    other = module.HistoryPages(
        pages.source,
        pages.directory,
        cursor_key=b"x" * 32,
        scope="other-ledger",
        clock=lambda: 1000.0,
    )
    with pytest.raises(ValueError):
        await other.page(limit=2, cursor=page["next_cursor"])
