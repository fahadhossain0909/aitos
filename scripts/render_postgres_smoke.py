"""Temporary Render endpoint exercising the real AITOS PostgreSQL adapters."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse


async def _write_and_read() -> dict[str, Any]:
    from aitos.data.postgres_repository import PostgresMarketDataRepository
    from aitos.journal.models import JournalEntry, JournalEntryType
    from aitos.journal.postgres_repository import PostgresJournalRepository

    dsn = os.environ["DATABASE_URL"]
    market = PostgresMarketDataRepository(dsn, min_size=1, max_size=2)
    journal = PostgresJournalRepository(dsn, min_size=1, max_size=2)
    try:
        # Both real AITOS adapters apply the same versioned, transactional
        # migration set during initialization.
        await asyncio.gather(market.initialize({}), journal.initialize({}))

        opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
        kline = SimpleNamespace(
            symbol="AITOSSMOKEUSDT",
            timeframe="1m",
            open_time=opened,
            close_time=opened,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            quote_volume=1005.0,
            trades_count=3,
            taker_buy_volume=6.0,
            taker_buy_quote_volume=603.0,
        )
        await market.save_kline(kline)
        klines = await market.get_recent_klines("AITOSSMOKEUSDT", "1m", limit=5)
        if not klines or float(klines[0]["close"]) != 100.5:
            raise RuntimeError("AITOS market adapter round-trip failed")

        trade_id = "render-smoke:trade"
        decision_id = "render-smoke:decision"
        await journal.save_trade_snapshot(
            {"trade_id": trade_id, "symbol": "AITOSSMOKEUSDT", "state": "OPEN"}
        )
        await journal.save_journal_entry(
            JournalEntry(
                entry_id="render-smoke:journal-entry",
                trade_id=trade_id,
                entry_type=JournalEntryType.PRE_TRADE,
                market_context={"symbol": "AITOSSMOKEUSDT", "source": "render-smoke"},
            )
        )
        await journal.save_decision(
            decision_id, {"candidate_id": "render-smoke:candidate"}
        )
        await journal.link_trade(
            decision_id, {"trade_id": trade_id, "symbol": "AITOSSMOKEUSDT"}
        )
        await journal.attribute_outcome(
            decision_id,
            {
                "trade_id": trade_id,
                "pnl": 0.5,
                "risk_amount_usd": 0.25,
                "entry_time": opened.isoformat(),
                "exit_time": opened.isoformat(),
            },
        )
        entries = await journal.get_journal_entries_for_trade(trade_id)
        records = await journal.get_records(decision_id)
        record_types = {row["record_type"] for row in records}
        if (
            len(entries) != 1
            or not {"DECISION", "TRADE_LINK", "OUTCOME"} <= record_types
        ):
            raise RuntimeError("AITOS operational journal round-trip failed")

        async with market._pool.acquire() as conn:
            schema_version = await conn.fetchval(
                "SELECT max(version) FROM aitos_schema_migrations"
            )
        if schema_version != 1:
            raise RuntimeError("AITOS PostgreSQL migration version is unexpected")

        return {
            "check": "aitos_postgres_adapter_round_trip",
            "status": "ok",
            "schema_version": int(schema_version),
            "market_data": "ok",
            "operational_journal": "ok",
        }
    finally:
        await asyncio.gather(market.shutdown(), journal.shutdown())


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
            result = asyncio.run(_write_and_read())
            self._respond(
                200,
                {"status": "ok", "database": "reachable", "adapter_test": result},
            )
        except Exception as exc:
            print(f"AITOS PostgreSQL adapter smoke failed: {type(exc).__name__}")
            self._respond(503, {"status": "unavailable"})

    def log_message(self, format: str, *args: Any) -> None:
        print(
            f"HTTP {self.command} {urlparse(self.path).path} "
            f"{args[1] if len(args) > 1 else ''}"
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"AITOS PostgreSQL adapter smoke service listening on port {port}")
    server.serve_forever()
