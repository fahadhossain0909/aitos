"""Loopback-only gateway security contracts; dummy credentials, no exchange/account."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_gateway import (
    DEFAULT_WORKSPACE_LAYOUT,
    GATEWAY_STATE,
    GatewayConfig,
    create_gateway,
    normalize_origin,
)

PASSWORD = "test-only-operator-password-not-a-real-credential"
TOKEN = "test-only-backend-bearer-" + "x" * 32
ORIGIN = "http://127.0.0.1:8780"


@pytest.mark.parametrize(
    ("password", "valid"),
    [
        ("a" * 23, False),
        ("a" * 24, True),
        ("λ" * 11, False),
        ("λ" * 12, True),
        ("a" * 1024, True),
        ("a" * 1025, False),
        (None, False),
    ],
)
def test_operator_password_validation_uses_utf8_byte_contract(password, valid):
    from aitos.console_gateway import is_valid_operator_password

    assert is_valid_operator_password(password) is valid


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<html>operator console</html>")
    (root / "assets").mkdir()
    (root / "assets" / "app.js").write_text('console.log("public asset")')
    seen = []
    release = asyncio.Event()
    entered = asyncio.Event()

    async def backend(request):
        seen.append(
            dict(
                method=request.method,
                path=request.path,
                headers=dict(request.headers),
                query=list(request.query.items()),
            )
        )
        if request.query.get("symbol") == "hold":
            if len(seen) == 4:
                entered.set()
            await release.wait()
        if request.query.get("symbol") == "redirect":
            raise web.HTTPFound("/private")
        if request.query.get("symbol") == "secret_error":
            return web.Response(text="private-backend-secret", status=500)
        if request.query.get("symbol") == "not_json":
            return web.Response(text="not-json")
        if request.query.get("symbol") == "large":
            return web.json_response({"body": "x" * (2 * 1024 * 1024)})
        if request.query.get("symbol") == "duplicate":
            return web.Response(
                text='{"value":1,"value":2}', content_type="application/json"
            )
        if request.query.get("symbol") == "nonfinite":
            return web.Response(text='{"value":NaN}', content_type="application/json")
        if request.query.get("symbol") == "encoded":
            return web.Response(
                body=b"not-a-decoded-body",
                content_type="application/json",
                headers={"Content-Encoding": "gzip"},
            )
        response = web.json_response(
            {"schema_version": 1, "available": True, "source": "fixture"}
        )
        response.set_cookie("untrusted_upstream_cookie", "must-not-forward")
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", backend)
    upstream = TestServer(app)
    await upstream.start_server()

    def prepare_test_directory(path, **_):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return path

    monkeypatch.setattr(
        "aitos.console_gateway.prepare_durable_directory", prepare_test_directory
    )
    config = GatewayConfig(
        ORIGIN,
        str(upstream.make_url("")).rstrip("/"),
        root,
        TOKEN,
        session_seconds=120,
        workspace_file=tmp_path / "config" / "workspace-layout.json",
    )
    clock = SimpleNamespace(now=1000.0)
    gateway = create_gateway(config, password=PASSWORD, clock=lambda: clock.now)
    client = TestClient(TestServer(gateway), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    yield SimpleNamespace(
        client=client,
        state=gateway[GATEWAY_STATE],
        seen=seen,
        clock=clock,
        config=config,
        root=root,
        release=release,
        entered=entered,
    )
    await client.close()
    await upstream.close()


async def login(h, password=PASSWORD, **kwargs):
    return await h.client.post(
        "/operator/v1/login",
        json={"password": password},
        headers={"Origin": ORIGIN},
        **kwargs,
    )


async def test_workspace_layout_is_authenticated_and_survives_gateway_restart(
    harness,
):
    h = harness
    assert (await h.client.get("/operator/v1/workspace-layout")).status == 401
    await login(h)

    initial = await h.client.get("/operator/v1/workspace-layout")
    assert initial.status == 200
    assert initial.headers["Cache-Control"] == "no-store"
    initial_etag = initial.headers["ETag"]
    assert len(initial_etag) == 66
    assert await initial.json() == DEFAULT_WORKSPACE_LAYOUT

    updated = {
        **DEFAULT_WORKSPACE_LAYOUT,
        "activeWorkspace": "risk",
        "panels": {
            **DEFAULT_WORKSPACE_LAYOUT["panels"],
            "risk": ["audit", "exposure"],
        },
    }
    saved = await h.client.put(
        "/operator/v1/workspace-layout",
        json=updated,
        headers={"Origin": ORIGIN, "If-Match": initial_etag},
    )
    assert saved.status == 200
    assert await saved.json() == updated
    assert h.config.workspace_file.is_file()

    restarted = create_gateway(h.config, password=PASSWORD)
    client = TestClient(TestServer(restarted), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    try:
        await client.post(
            "/operator/v1/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        restored = await client.get("/operator/v1/workspace-layout")
        assert restored.status == 200
        assert await restored.json() == updated
    finally:
        await client.close()


async def test_workspace_layout_compare_and_swap_between_windows(harness):
    h = harness
    await login(h)
    first = await h.client.get("/operator/v1/workspace-layout")
    second = await h.client.get("/operator/v1/workspace-layout")
    stale_etag = second.headers["ETag"]
    updated = {**DEFAULT_WORKSPACE_LAYOUT, "activeWorkspace": "risk"}
    saved = await h.client.put(
        "/operator/v1/workspace-layout",
        json=updated,
        headers={"Origin": ORIGIN, "If-Match": first.headers["ETag"]},
    )
    assert saved.status == 200 and saved.headers["ETag"] != stale_etag
    stale = {**DEFAULT_WORKSPACE_LAYOUT, "activeWorkspace": "developer"}
    conflict = await h.client.put(
        "/operator/v1/workspace-layout",
        json=stale,
        headers={"Origin": ORIGIN, "If-Match": stale_etag},
    )
    assert conflict.status == 409
    assert conflict.headers["ETag"] == saved.headers["ETag"]
    assert await conflict.json() == updated

    restored = await h.client.put(
        "/operator/v1/workspace-layout",
        json=DEFAULT_WORKSPACE_LAYOUT,
        headers={"Origin": ORIGIN, "If-Match": saved.headers["ETag"]},
    )
    assert restored.status == 200
    aba_conflict = await h.client.put(
        "/operator/v1/workspace-layout",
        json=stale,
        headers={"Origin": ORIGIN, "If-Match": stale_etag},
    )
    assert aba_conflict.status == 409
    assert await aba_conflict.json() == DEFAULT_WORKSPACE_LAYOUT


async def test_workspace_layout_rejects_invalid_dock_geometry(harness):
    h = harness
    await login(h)
    initial = await h.client.get("/operator/v1/workspace-layout")
    invalid = {
        **DEFAULT_WORKSPACE_LAYOUT,
        "panelGeometry": {
            **DEFAULT_WORKSPACE_LAYOUT["panelGeometry"],
            "trading": {"charts": {"width": 3, "height": 1}},
        },
    }
    response = await h.client.put(
        "/operator/v1/workspace-layout",
        json=invalid,
        headers={"Origin": ORIGIN, "If-Match": initial.headers["ETag"]},
    )
    assert response.status == 400
    restored = await h.client.get("/operator/v1/workspace-layout")
    assert await restored.json() == DEFAULT_WORKSPACE_LAYOUT


async def test_legacy_workspace_file_migrates_to_geometry_schema(harness):
    h = harness
    h.config.workspace_file.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "activeWorkspace": "quant",
        "panels": DEFAULT_WORKSPACE_LAYOUT["panels"],
    }
    h.config.workspace_file.write_text(json.dumps(legacy), encoding="utf-8")
    await login(h)
    response = await h.client.get("/operator/v1/workspace-layout")
    assert response.status == 200
    restored = await response.json()
    assert restored["version"] == 2
    assert restored["activeWorkspace"] == "quant"
    assert restored["panelGeometry"] == DEFAULT_WORKSPACE_LAYOUT["panelGeometry"]


async def test_workspace_layout_requires_revision_for_mutation(harness):
    h = harness
    await login(h)
    response = await h.client.put(
        "/operator/v1/workspace-layout",
        json=DEFAULT_WORKSPACE_LAYOUT,
        headers={"Origin": ORIGIN},
    )
    assert response.status == 428


async def test_workspace_layout_rejects_unknown_panels_without_overwriting(harness):
    h = harness
    await login(h)
    initial = await h.client.get("/operator/v1/workspace-layout")
    original = await initial.json()
    etag = initial.headers["ETag"]
    invalid = {
        **original,
        "panels": {
            **original["panels"],
            "trading": ["charts", "shell.exec"],
        },
    }
    response = await h.client.put(
        "/operator/v1/workspace-layout",
        json=invalid,
        headers={"Origin": ORIGIN, "If-Match": etag},
    )
    assert response.status == 400
    restored = await h.client.get("/operator/v1/workspace-layout")
    assert await restored.json() == original


async def test_sign_in_cookie_and_server_side_bearer_boundary(harness):
    h = harness
    assert (await h.client.get("/console/v1/snapshot")).status == 401
    response = await login(h)
    assert response.status == 200
    cookie = response.cookies["aitos_local_operator"]
    assert (
        cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/"
    )
    assert not cookie["secure"] and cookie["max-age"] == "120"
    assert cookie.value.encode() not in h.state.sessions
    assert hashlib.sha256(cookie.value.encode()).digest() in h.state.sessions
    result = await h.client.get(
        "/console/v1/history?symbol=BTCUSDT",
        headers={
            "Authorization": "Bearer untrusted-browser-token",
            "X-Forwarded-For": "foreign",
            "Cookie": f"aitos_local_operator={cookie.value}; exchange_secret=not-forwarded",
        },
    )
    assert result.status == 200
    assert TOKEN not in await result.text()
    assert not result.cookies
    assert h.seen[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "Cookie" not in h.seen[-1]["headers"]
    assert "X-Forwarded-For" not in h.seen[-1]["headers"]
    assert h.seen[-1]["query"] == [("symbol", "BTCUSDT")]


async def test_session_status_does_not_refresh_absolute_expiry(harness):
    h = harness
    await login(h)
    h.clock.now += 60
    response = await h.client.get("/operator/v1/session")
    assert (await response.json())["expires_in_seconds"] == 60
    h.clock.now += 61
    assert (await (await h.client.get("/operator/v1/session")).json())[
        "authenticated"
    ] is False
    assert (await h.client.get("/console/v1/snapshot")).status == 401


async def test_logout_revokes_captured_cookie(harness):
    h = harness
    response = await login(h)
    cookie = response.cookies["aitos_local_operator"].value
    assert (
        await h.client.post("/operator/v1/logout", headers={"Origin": ORIGIN})
    ).status == 204
    assert not h.state.sessions
    assert (
        await h.client.get(
            "/console/v1/history", headers={"Cookie": f"aitos_local_operator={cookie}"}
        )
    ).status == 401


async def test_new_login_rotates_previous_session(harness):
    h = harness
    first = (await login(h)).cookies["aitos_local_operator"].value
    second = (await login(h)).cookies["aitos_local_operator"].value
    assert (
        first != second
        and h.state.remaining(first) == 0
        and h.state.remaining(second) > 0
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.invalid"},
        {"Origin": "null"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
    ],
)
async def test_mutations_require_exact_same_origin(harness, headers):
    response = await harness.client.post(
        "/operator/v1/login", json={"password": PASSWORD}, headers=headers
    )
    assert response.status == 403 and not harness.state.sessions


async def test_foreign_host_and_forwarded_host_not_trusted(harness):
    response = await harness.client.get(
        "/operator/v1/session",
        headers={"Host": "evil.invalid", "X-Forwarded-Host": "127.0.0.1:8780"},
    )
    assert response.status == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/console/v1/snapshot"),
        ("delete", "/console/v1/history"),
        ("put", "/console/v1/performance"),
        ("get", "/console/v1/order"),
        ("get", "/console/v1/../../health"),
        ("get", "/console/v1/https://evil.invalid"),
    ],
)
async def test_no_order_or_arbitrary_backend_route_can_be_forwarded(
    harness, method, path
):
    h = harness
    await login(h)
    h.seen.clear()
    response = await getattr(h.client, method)(path, headers={"Origin": ORIGIN})
    assert response.status in {404, 405}
    assert not h.seen


@pytest.mark.parametrize(
    "fixture",
    [
        "redirect",
        "secret_error",
        "not_json",
        "large",
        "duplicate",
        "nonfinite",
        "encoded",
    ],
)
async def test_upstream_failure_is_sanitized_bounded_and_no_redirect_followed(
    harness, fixture
):
    h = harness
    await login(h)
    response = await h.client.get("/console/v1/history?symbol=" + fixture)
    assert response.status == 502
    assert "private-backend-secret" not in await response.text()
    assert len(h.seen) == 1


async def test_global_login_attempt_limit_is_bounded_and_expires(harness):
    h = harness
    for _ in range(10):
        assert (await login(h, password="wrong-test-password")).status == 401
    assert (await login(h)).status == 429
    assert len(h.state.attempts) == 10
    h.clock.now += 61
    assert (await login(h)).status == 200


async def test_rate_inventory_and_session_inventory_stay_bounded(harness):
    h = harness
    for _ in range(20):
        h.state.issue()
    assert len(h.state.sessions) == h.config.max_sessions
    h.clock.now += 121
    h.state.prune()
    assert not h.state.sessions


async def test_static_bundle_is_public_but_traversal_and_hidden_files_are_not(
    harness, tmp_path
):
    h = harness
    secret = tmp_path / "private.txt"
    secret.write_text("private-test-fixture")
    (h.root / "assets" / "escape.js").symlink_to(secret)
    assert (await h.client.get("/")).status == 200
    assert (await h.client.get("/assets/app.js")).status == 200
    for path in [
        "/assets/escape.js",
        "/.env",
        "/assets/.hidden.js",
        "/assets/../../private.txt",
        "/not-a-console-route",
    ]:
        response = await h.client.get(path)
        assert (
            response.status == 404
            and "private-test-fixture" not in await response.text()
        )


async def test_security_headers_apply_to_auth_and_error_responses(harness):
    for path in ["/", "/console/v1/snapshot", "/operator/v1/session", "/.env"]:
        response = await harness.client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in response.headers


async def test_body_size_and_content_type_are_bounded(harness):
    h = harness
    assert (
        await h.client.post(
            "/operator/v1/login",
            data="password=" + PASSWORD,
            headers={"Origin": ORIGIN},
        )
    ).status == 415
    assert (
        await h.client.post(
            "/operator/v1/login",
            data="x" * 5000,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
    ).status == 413


async def test_passwords_and_bearers_are_not_logged(harness, caplog):
    h = harness
    caplog.set_level("INFO")
    await login(h)
    await login(h, password="wrong-sensitive-test-password")
    assert PASSWORD not in caplog.text and TOKEN not in caplog.text
    assert "wrong-sensitive-test-password" not in caplog.text


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?token=x",
        "https://example.com#fragment",
        "ftp://localhost",
        "https://example.com:99999",
        "https://example.com\\evil",
    ],
)
def test_invalid_or_insecure_origins_rejected(tmp_path, origin):
    (tmp_path / "index.html").write_text("test")
    with pytest.raises(ValueError):
        GatewayConfig(origin, "http://127.0.0.1:8765", tmp_path)


@pytest.mark.parametrize("password", ["", "short"])
def test_missing_or_short_operator_credential_fails_closed(tmp_path, password):
    (tmp_path / "index.html").write_text("test")
    config = GatewayConfig(ORIGIN, "http://127.0.0.1:8765", tmp_path)
    with pytest.raises(ValueError):
        create_gateway(config, password=password)


def test_origin_normalization():
    assert normalize_origin("https://EXAMPLE.com:443/") == "https://example.com"
    assert normalize_origin("http://[::1]:8780") == "http://[::1]:8780"


async def test_https_cookie_is_secure_host_only_and_hsts_is_set(harness):
    config = replace(harness.config, public_origin="https://operator.example")
    app = create_gateway(config, password=PASSWORD)
    async with TestClient(
        TestServer(app), headers={"Host": "operator.example"}
    ) as client:
        response = await client.post(
            "/operator/v1/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://operator.example"},
        )
        assert response.status == 200
        cookie = response.cookies["__Host-aitos_operator"]
        assert (
            cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict"
        )
        assert cookie["path"] == "/" and not cookie["domain"]
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


async def test_fabricated_cookie_or_browser_bearer_does_not_grant_access(harness):
    response = await harness.client.get(
        "/console/v1/snapshot",
        headers={
            "Cookie": "aitos_local_operator=" + "x" * 43,
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    assert response.status == 401 and not harness.seen


async def test_get_logout_cannot_be_used_as_a_cross_site_mutation(harness):
    h = harness
    await login(h)
    assert (await h.client.get("/operator/v1/logout")).status in {404, 405}
    assert (await h.client.get("/console/v1/snapshot")).status == 200


async def test_upstream_concurrency_is_fail_fast_and_bounded(harness):
    h = harness
    await login(h)
    tasks = [
        asyncio.create_task(h.client.get("/console/v1/history?symbol=hold"))
        for _ in range(4)
    ]
    try:
        await asyncio.wait_for(h.entered.wait(), timeout=5)
        assert (await h.client.get("/console/v1/snapshot")).status == 429
        assert len(h.seen) == 4
    finally:
        h.release.set()
        responses = await asyncio.gather(*tasks)
        for response in responses:
            assert response.status == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com%40evil.invalid",
        "https://a..example.com",
        "http://[::1%25lo]:8780",
    ],
)
def test_encoded_or_ambiguous_hosts_are_rejected(origin):
    with pytest.raises(ValueError):
        normalize_origin(origin)


@pytest.mark.parametrize(
    "path",
    [
        "/console/v1/snapshot?token=forbidden",
        "/console/v1/performance?limit=100",
        "/console/v1/history?symbol=BTCUSDT&symbol=ETHUSDT",
        "/console/v1/research/plan?password=forbidden",
    ],
)
async def test_unknown_or_duplicate_proxy_query_fields_are_not_forwarded(harness, path):
    h = harness
    await login(h)
    h.seen.clear()
    assert (await h.client.get(path)).status == 400
    assert not h.seen


async def test_built_source_maps_and_nonpublic_assets_are_not_served(harness):
    h = harness
    for name in ["source.js.map", "private.txt", "copied.env"]:
        (h.root / "assets" / name).write_text("not-public-test-data")
        response = await h.client.get("/assets/" + name)
        assert (
            response.status == 404
            and "not-public-test-data" not in await response.text()
        )
