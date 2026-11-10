from datetime import datetime, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_api import register_console_routes
from aitos.execution.operator_protection import (
    ExportedProtectionObserver,
    ProtectionObservationUnavailable,
)


def capture():
    return dict(
        intents=[
            dict(
                scope="synthetic",
                client_id="sensitive-client-id",
                state="PROTECTED",
                request_json=dict(
                    symbol="BTCUSDT",
                    side="LONG",
                    quantity=1,
                    reference_price=90,
                    order_type="STOP_MARKET",
                    reduce_only=True,
                ),
            )
        ],
        evidence=dict(
            scope="synthetic",
            complete=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            symbols=["BTCUSDT"],
            orders=[],
        ),
    )


async def test_missing_protection_never_implies_safe_to_resume():
    async def read():
        return capture()

    result = await ExportedProtectionObserver(read, scope="synthetic").observe()
    assert result["counts"]["not_in_open_inventory"] == 1
    assert result["safe_to_resume"] is result["account_exposure_verified"] is False
    assert result["automatic_actions"] == []
    assert "sensitive-client-id" not in str(result)
    assert len(result["findings"][0]["id"]) == 24


@pytest.mark.parametrize(
    "kind", ["stale", "incomplete", "cross_scope", "invalid_symbol", "error"]
)
async def test_capture_failure_redacted_and_no_synthetic_zero_counts(kind):
    async def read():
        data = capture()
        if kind == "stale":
            data["evidence"]["captured_at"] = "2020-01-01T00:00:00+00:00"
        elif kind == "incomplete":
            data["evidence"]["complete"] = False
        elif kind == "cross_scope":
            data["evidence"]["scope"] = "other"
        elif kind == "invalid_symbol":
            data["intents"][0]["request_json"]["symbol"] = "<script>"
        else:
            raise RuntimeError("secret-provider-key")
        return data

    with pytest.raises(ProtectionObservationUnavailable) as error:
        await ExportedProtectionObserver(read, scope="synthetic").observe()
    assert "secret" not in str(error.value)


async def test_route_auth_read_only_and_unattached():
    async def read():
        return capture()

    observer = ExportedProtectionObserver(read, scope="synthetic")
    with pytest.raises(ValueError):
        register_console_routes(web.Application(), protection_observer=observer)
    token = "synthetic-token-" + "x" * 32
    app = web.Application()
    register_console_routes(app, token=token, protection_observer=observer)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/protection")).status == 401
        headers = {"Authorization": "Bearer " + token}
        assert (
            await client.get("/console/v1/protection?scope=other", headers=headers)
        ).status == 400
        assert (
            await client.post("/console/v1/protection", headers=headers)
        ).status == 405
        response = await client.get("/console/v1/protection", headers=headers)
        assert (
            response.status == 200 and response.headers["Cache-Control"] == "no-store"
        )
        assert (await response.json())["counts"]["not_in_open_inventory"] == 1
    app = web.Application()
    register_console_routes(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/console/v1/protection")
        assert response.status == 503 and (await response.json())["available"] is False


async def test_concurrent_capture_fails_closed_and_cancellation_releases_lock():
    import asyncio

    started = asyncio.Event()

    async def read():
        started.set()
        await asyncio.Event().wait()

    observer = ExportedProtectionObserver(read, scope="synthetic")
    task = asyncio.create_task(observer.observe())
    await started.wait()
    with pytest.raises(ProtectionObservationUnavailable):
        await observer.observe()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not observer._lock.locked()


async def test_oversized_inventory_rejected():
    async def read():
        data = capture()
        data["evidence"]["symbols"] = ["BTCUSDT"] * 2001
        return data

    with pytest.raises(ProtectionObservationUnavailable):
        await ExportedProtectionObserver(read, scope="synthetic").observe()
