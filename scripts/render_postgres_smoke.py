"""Temporary, authenticated Render PostgreSQL connectivity smoke service."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


async def _connect_and_ping() -> None:
    import asyncpg

    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


async def _write_and_read() -> dict[str, Any]:
    import asyncpg

    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    key = f"render-smoke:{uuid.uuid4().hex}"
    payload = {"check": "postgres_write_read", "status": "ok"}
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aitos_render_smoke (
                idempotency_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO aitos_render_smoke (idempotency_key, payload)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            key,
            json.dumps(payload),
        )
        row = await conn.fetchrow(
            """
            SELECT payload::text AS payload
            FROM aitos_render_smoke
            WHERE idempotency_key = $1
            """,
            key,
        )
        if row is None:
            raise RuntimeError("PostgreSQL read-after-write returned no row")
        return json.loads(row["payload"])
    finally:
        await conn.close()


class Handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/health":
            self._respond(404, {"status": "not_found"})
            return
        try:
            asyncio.run(_connect_and_ping())
            self._respond(200, {"status": "ok", "database": "reachable"})
        except Exception as exc:
            print(f"PostgreSQL smoke health failed: {type(exc).__name__}")
            self._respond(503, {"status": "unavailable"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/verify":
            self._respond(404, {"status": "not_found"})
            return
        expected = os.environ.get("SMOKE_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(
            supplied, f"Bearer {expected}"
        ):
            self._respond(403, {"status": "forbidden"})
            return
        try:
            result = asyncio.run(_write_and_read())
            self._respond(200, {"status": "ok", "result": result})
        except Exception as exc:
            print(f"PostgreSQL smoke write/read failed: {type(exc).__name__}")
            self._respond(503, {"status": "write_read_failed"})

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid logging headers, tokens, query strings, or database details.
        print(f"HTTP {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"PostgreSQL smoke service listening on port {port}")
    server.serve_forever()
