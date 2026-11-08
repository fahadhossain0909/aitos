"""Opt-in real Chromium/built-bundle auth test. No external or account services."""

import asyncio
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aitos.console_api import register_console_routes
from aitos.console_gateway import GatewayConfig, create_gateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AITOS_RUN_OPERATOR_BROWSER") != "1",
        reason="opt-in built-bundle Chromium harness",
    ),
]


async def test_built_operator_browser_authentication(tmp_path):
    root = Path(__file__).resolve().parents[1]
    upstream_app = web.Application()
    from aitos.trading.revision_journal import RevisionJournal, Scope
    from aitos.trading.revision_projector import RevisionProjector

    class Sink:
        def __init__(self):
            self.rows = []

        async def lookup(self, batch):
            return list(self.rows)

        async def insert(self, batch):
            self.rows.extend(batch)

    journal = RevisionJournal(tmp_path / "built-browser-synthetic.sqlite")
    writer = journal.acquire(
        Scope("paper", "synthetic", "test-account", "test-run"),
        "test-writer",
        expected_generation=0,
    )
    for i in range(2):
        journal.append(
            writer,
            trade_id="synthetic-one",
            event_id=f"event-{i}",
            expected_revision=i,
            payload={
                "trade_id": "synthetic-one",
                "state": "position_opened",
                "quantity": 1,
            },
        )
    projector = RevisionProjector(journal, writer, Sink())
    assert (await projector.run_once())["status"] == "verified"
    upstream_token = "test-only-projection-bearer-" + ("x" * 32)
    register_console_routes(
        upstream_app, projection_observer=projector, token=upstream_token
    )
    upstream = TestServer(upstream_app)
    await upstream.start_server()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    clock = SimpleNamespace(now=1000.0)
    password = "test-only-isolated-browser-password-not-a-real-credential"
    app = create_gateway(
        GatewayConfig(
            origin,
            str(upstream.make_url("")).rstrip("/"),
            root / "desktop/dist",
            upstream_token=upstream_token,
            session_seconds=60,
        ),
        password=password,
        clock=lambda: clock.now,
    )
    server = TestServer(
        app, host="127.0.0.1", port=port, socket_factory=lambda *args: sock
    )
    await server.start_server()
    env = {
        **os.environ,
        "AITOS_BROWSER_GATEWAY_URL": origin,
        "AITOS_BROWSER_GATEWAY_PASSWORD": password,
    }
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            "operator-smoke.mjs",
            cwd=root / "desktop",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines = []

        async def collect():
            async for raw in process.stdout:
                line = raw.decode()
                lines.append(line)
                if line.strip() == "ADVANCE_TEST_CLOCK":
                    clock.now += 61
            return await process.wait()

        result = await asyncio.wait_for(collect(), timeout=65)
        assert result == 0, "".join(lines)
        assert any("PASS:" in line for line in lines)
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        await server.close()
        await upstream.close()
