from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.trading import account_timeline as module
from aitos.console_api import register_console_routes


def observation(value="10"):
    return dict(
        schema_version=1,
        source="binance_signed_rest",
        controls="read_only",
        observed_at=datetime.now(timezone.utc).isoformat(),
        totals={key: value for key in module.FIELDS},
        apiKey="never-copy",
    )


@pytest.fixture
def timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "prepare_durable_directory", lambda path: path)
    return module.AccountTimeline(
        tmp_path, account="synthetic-account", cursor_key=b"x" * 32
    )


def test_high_water_keyset_cut_excludes_concurrent_appends(timeline):
    for i in range(5):
        timeline.append(observation(str(i)))
    first = timeline.page(limit=2)
    assert [row["id"] for row in first["rows"]] == [5, 4]
    timeline.append(observation("999"))
    second = timeline.page(limit=2, cursor=first["next_cursor"])
    third = timeline.page(limit=2, cursor=second["next_cursor"])
    assert [row["id"] for row in second["rows"] + third["rows"]] == [3, 2, 1]
    assert third["next_cursor"] is None
    assert "never-copy" not in str(first)
    with sqlite3.connect(timeline.path) as db:
        assert "never-copy" not in str(
            db.execute("SELECT * FROM observations").fetchall()
        )


def test_reopen_preserves_data_and_cursor_scope(timeline):
    for _ in range(3):
        timeline.append(observation())
    cursor = timeline.page(limit=1)["next_cursor"]
    reopened = module.AccountTimeline(
        timeline.directory, account=timeline.account, cursor_key=b"x" * 32
    )
    assert reopened.page(limit=1, cursor=cursor)["rows"][0]["id"] == 2
    with pytest.raises(ValueError):
        module.AccountTimeline(
            timeline.directory, account="other", cursor_key=b"x" * 32
        )
    wrong_key = module.AccountTimeline(
        timeline.directory, account=timeline.account, cursor_key=b"y" * 32
    )
    with pytest.raises(ValueError):
        wrong_key.page(limit=1, cursor=cursor)
    with pytest.raises(ValueError):
        timeline.page(limit=2, cursor=cursor)
    with pytest.raises(ValueError):
        timeline.page(limit=1, cursor=cursor + "x")


@pytest.mark.parametrize("change", ["stale", "missing", "nan", "source"])
def test_invalid_observations_do_not_persist(timeline, change):
    value = observation()
    if change == "stale":
        value["observed_at"] = "2020-01-01T00:00:00+00:00"
    elif change == "missing":
        del value["totals"]["availableBalance"]
    elif change == "nan":
        value["totals"]["availableBalance"] = "NaN"
    else:
        value["source"] = "sample"
    with pytest.raises(ValueError):
        timeline.append(value)
    assert timeline.page()["rows"] == []


async def test_wrapper_persists_only_successful_observations(timeline):
    async def observe():
        return observation()

    wrapper = module.RecordedAccountObserver(SimpleNamespace(observe=observe), timeline)
    await wrapper.observe()
    assert len(timeline.page()["rows"]) == 1

    async def failed():
        raise RuntimeError("synthetic provider failure")

    wrapper.provider = SimpleNamespace(observe=failed)
    with pytest.raises(RuntimeError):
        await wrapper.observe()
    assert len(timeline.page()["rows"]) == 1


async def test_authenticated_timeline_route_and_query_contract(timeline):
    for _ in range(3):
        timeline.append(observation())
    token = "synthetic-token-" + "x" * 32
    app = web.Application()
    register_console_routes(
        app, token=token, account_observer=SimpleNamespace(timeline=timeline)
    )
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/account/timeline")).status == 401
        headers = {"Authorization": "Bearer " + token}
        response = await client.get(
            "/console/v1/account/timeline?limit=1", headers=headers
        )
        assert (
            response.status == 200 and response.headers["Cache-Control"] == "no-store"
        )
        page = await response.json()
        response = await client.get(
            "/console/v1/account/timeline",
            params={"limit": "1", "cursor": page["next_cursor"]},
            headers=headers,
        )
        assert (await response.json())["rows"][0]["id"] == 2
        for query in ("limit=0", "limit=1&limit=2", "cursor=bad", "account=other"):
            assert (
                await client.get(
                    "/console/v1/account/timeline?" + query, headers=headers
                )
            ).status == 400
        assert (
            await client.post("/console/v1/account/timeline", headers=headers)
        ).status == 405


def test_database_rejects_accidental_mutation(timeline):
    timeline.append(observation())
    with sqlite3.connect(timeline.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM observations")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE observations SET totals='{}'")
    assert len(timeline.page()["rows"]) == 1
