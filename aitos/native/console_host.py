"""Managed loopback gateway lifetime, shared by Windows GUI and frozen smoke.

No runner launch, shell, credential argv/env or public listener. Thread failure is
observable; stop requests made during startup are retained, not lost.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

from aiohttp import web

from aitos.console_gateway import GatewayConfig, create_gateway


class LocalConsoleHost:
    def __init__(
        self, dist, *, upstream_port=None, upstream_origin=None, password, token
    ):
        from aitos.console_capabilities import RuntimeEndpoint

        if (upstream_port is None) == (upstream_origin is None):
            raise ValueError("select exactly one explicit runtime endpoint")
        if upstream_port is not None and (
            type(upstream_port) is not int or not 1 <= upstream_port <= 65535
        ):
            raise ValueError("local upstream port required")
        self._endpoint = RuntimeEndpoint(
            upstream_origin
            if upstream_origin is not None
            else f"http://127.0.0.1:{upstream_port}"
        )
        self._dist = Path(dist)
        self._port = upstream_port
        self._password = password
        self._token = token
        self.ready = threading.Event()
        self.closed = threading.Event()
        self._closing = threading.Event()
        self._thread = None
        self._loop = None
        self._stop = None
        self.origin = None
        self.failed = False

    def start(self):
        if self._thread is not None or self._closing.is_set():
            raise RuntimeError("host cannot restart")
        self._thread = threading.Thread(target=self._worker, name="aitos-local-gateway")
        self._thread.start()

    def _worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception:
            self.failed = True
        finally:
            # aiohttp cleanup has completed before loop closure/closed signal.
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                self.failed = True
            finally:
                loop.close()
                self._password = self._token = None
                self.ready.set()
                self.closed.set()

    async def _serve(self):
        sock = None
        runner = None
        try:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            if self._closing.is_set():
                self._stop.set()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sock.setblocking(False)
            self.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            config = GatewayConfig(
                self.origin,
                self._endpoint.origin,
                self._dist,
                upstream_token=self._token,
            )
            runner = web.AppRunner(
                create_gateway(config, password=self._password), access_log=None
            )
            await runner.setup()
            await web.SockSite(runner, sock).start()
            self.ready.set()
            await self._stop.wait()
        except Exception:
            self.failed = True
        finally:
            try:
                if runner is not None:
                    await runner.cleanup()
            finally:
                if sock is not None:
                    sock.close()

    def stop(self, timeout=10):
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError("bounded join required")
        self._closing.set()
        if self._thread is None:
            self.closed.set()
            return True
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot join current thread")
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not self.closed.is_set():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                # Loop may finish between reading state and submitting. Join is
                # still mandatory: do not infer successful shutdown from this.
                self.failed = self.failed or not loop.is_closed()
        self._thread.join(timeout)
        return self.closed.is_set() and not self._thread.is_alive()


async def exercise_local_host(dist):
    """Synthetic actual TCP/auth smoke for installed binaries; no real credentials."""
    import secrets

    from aiohttp import ClientSession, ClientTimeout, CookieJar

    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app = web.Application()
    seen = []

    async def snapshot(request):
        if request.headers.get("Authorization") != "Bearer " + token:
            raise web.HTTPUnauthorized()
        seen.append(True)
        return web.json_response(
            dict(schema_version=1, available=False, source="synthetic_native_smoke")
        )

    app.router.add_get("/console/v1/snapshot", snapshot)
    upstream = web.AppRunner(app, access_log=None)
    await upstream.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    host = LocalConsoleHost(dist, upstream_port=port, password=password, token=token)
    try:
        await web.SockSite(upstream, sock).start()
        host.start()
        if not await asyncio.to_thread(host.ready.wait, 10) or host.failed:
            raise RuntimeError("gateway startup failed")
        async with ClientSession(
            cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=5)
        ) as client:
            async with client.get(host.origin + "/console/v1/snapshot") as response:
                if response.status != 401:
                    raise RuntimeError("unauthenticated access allowed")
            async with client.post(
                host.origin + "/operator/v1/login",
                json={"password": password},
                headers={"Origin": host.origin},
            ) as response:
                if response.status != 200:
                    raise RuntimeError("native login failed")
                cookie = response.cookies.get("aitos_local_operator")
                if (
                    cookie is None
                    or not cookie["httponly"]
                    or cookie["samesite"] != "Strict"
                ):
                    raise RuntimeError("cookie policy failed")
            async with client.get(host.origin + "/console/v1/snapshot") as response:
                body = await response.json()
                if (
                    response.status != 200
                    or body.get("source") != "synthetic_native_smoke"
                ):
                    raise RuntimeError("proxy evidence failed")
                if token in str(body) or response.headers.get("Authorization"):
                    raise RuntimeError("backend credential exposed")
            async with client.post(
                host.origin + "/operator/v1/logout", headers={"Origin": host.origin}
            ) as response:
                if response.status != 204:
                    raise RuntimeError("revocation failed")
            async with client.get(host.origin + "/console/v1/snapshot") as response:
                if response.status != 401:
                    raise RuntimeError("revocation did not take effect")
        if len(seen) != 1:
            raise RuntimeError("unexpected upstream reads")
    finally:
        try:
            if not await asyncio.to_thread(host.stop):
                raise RuntimeError("gateway did not drain")
        finally:
            await upstream.cleanup()
            sock.close()
    return dict(
        status="passed",
        synthetic=True,
        live_authorized=False,
        production_certified=False,
    )
