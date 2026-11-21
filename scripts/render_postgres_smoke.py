"""PostgreSQL persistence probe for Render/Railway smoke services.

Health requests are strictly read-only. Seed records are written only when an
operator explicitly runs this script with --seed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

SMOKE_SYMBOL = "AITOSSMOKEUSDT"
SMOKE_TIMEFRAME = "1m"
SMOKE_CLOSE = 100.5
SMOKE_TRADE_ID = "render-smoke:trade"
SMOKE_DECISION_ID = "render-smoke:decision"
SMOKE_ENTRY_ID = "render-smoke:journal-entry"
SMOKE_OPENED = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed_once() -> dict[str, Any]:
    """Explicitly seed deterministic idempotent records; never called by health."""
    from aitos.data.postgres_repository import PostgresMarketDataRepository
    from aitos.journal.models import JournalEntry, JournalEntryType
    from aitos.journal.postgres_repository import PostgresJournalRepository

    dsn = os.environ["DATABASE_URL"]
    market = PostgresMarketDataRepository(dsn, min_size=1, max_size=2)
    journal = PostgresJournalRepository(dsn, min_size=1, max_size=2)
    try:
        await asyncio.gather(market.initialize({}), journal.initialize({}))
        kline = SimpleNamespace(
            symbol=SMOKE_SYMBOL,
            timeframe=SMOKE_TIMEFRAME,
            open_time=SMOKE_OPENED,
            close_time=SMOKE_OPENED,
            open=100.0,
            high=101.0,
            low=99.0,
            close=SMOKE_CLOSE,
            volume=10.0,
            quote_volume=1005.0,
            trades_count=3,
            taker_buy_volume=6.0,
            taker_buy_quote_volume=603.0,
        )
        await market.save_kline(kline)
        await journal.save_trade_snapshot(
            {"trade_id": SMOKE_TRADE_ID, "symbol": SMOKE_SYMBOL, "state": "OPEN"}
        )
        await journal.save_journal_entry(
            JournalEntry(
                entry_id=SMOKE_ENTRY_ID,
                trade_id=SMOKE_TRADE_ID,
                entry_type=JournalEntryType.PRE_TRADE,
                market_context={"symbol": SMOKE_SYMBOL, "source": "render-smoke"},
            )
        )
        await journal.save_decision(
            SMOKE_DECISION_ID, {"candidate_id": "render-smoke:candidate"}
        )
        await journal.link_trade(
            SMOKE_DECISION_ID,
            {"trade_id": SMOKE_TRADE_ID, "symbol": SMOKE_SYMBOL},
        )
        await journal.attribute_outcome(
            SMOKE_DECISION_ID,
            {
                "trade_id": SMOKE_TRADE_ID,
                "pnl": 0.5,
                "risk_amount_usd": 0.25,
                "entry_time": SMOKE_OPENED.isoformat(),
                "exit_time": SMOKE_OPENED.isoformat(),
            },
        )
        verified = await _verify_with_adapters(market, journal)
        return {
            **verified,
            "check": "aitos_postgres_persistence_seed",
            "write_performed": True,
        }
    finally:
        await asyncio.gather(market.shutdown(), journal.shutdown())


async def _verify_with_adapters(market: Any, journal: Any) -> dict[str, Any]:
    """Read and validate stable sentinels using already initialized adapters."""
    klines = await market.get_recent_klines(SMOKE_SYMBOL, SMOKE_TIMEFRAME, limit=5)
    matching_klines = [
        row
        for row in klines
        if float(row.get("close", 0.0)) == SMOKE_CLOSE and row.get("time") is not None
    ]
    if not matching_klines:
        raise RuntimeError("persisted market-data sentinel is missing or changed")

    entries = await journal.get_journal_entries_for_trade(SMOKE_TRADE_ID)
    records = await journal.get_records(SMOKE_DECISION_ID)
    trade_records = await journal.get_trade_records(SMOKE_TRADE_ID)
    record_types = {row["record_type"] for row in records}
    trade_types = {row["record_type"] for row in trade_records}
    snapshot_ok = any(
        row.get("record_type") == "TRADE_SNAPSHOT"
        and row.get("trade_id") == SMOKE_TRADE_ID
        and row.get("state") == "OPEN"
        for row in trade_records
    )
    if (
        len(entries) != 1
        or not {"DECISION", "TRADE_LINK", "OUTCOME"} <= record_types
        or not {"TRADE_SNAPSHOT", "JOURNAL_ENTRY", "TRADE_LINK"} <= trade_types
        or not snapshot_ok
    ):
        raise RuntimeError("persisted operational-journal sentinel is incomplete")

    async with market._pool.acquire() as conn:
        schema_version = await conn.fetchval(
            "SELECT max(version) FROM aitos_schema_migrations"
        )
    if schema_version != 1:
        raise RuntimeError("AITOS PostgreSQL migration version is unexpected")

    return {
        "check": "aitos_postgres_persistence_read_only",
        "status": "ok",
        "schema_version": int(schema_version),
        "market_data": "persisted",
        "operational_journal": "persisted",
        "write_performed": False,
    }


async def _verify_persisted() -> dict[str, Any]:
    """Verify pre-existing rows only; do not repair missing persistence evidence."""
    from aitos.data.postgres_repository import PostgresMarketDataRepository
    from aitos.journal.postgres_repository import PostgresJournalRepository

    dsn = os.environ["DATABASE_URL"]
    market = PostgresMarketDataRepository(dsn, min_size=1, max_size=2)
    journal = PostgresJournalRepository(dsn, min_size=1, max_size=2)
    try:
        await asyncio.gather(market.initialize({}), journal.initialize({}))
        return await _verify_with_adapters(market, journal)
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
            result = asyncio.run(_verify_persisted())
            self._respond(
                200,
                {"status": "ok", "database": "reachable", "adapter_test": result},
            )
        except Exception as exc:
            print(f"AITOS PostgreSQL persistence probe failed: {type(exc).__name__}")
            self._respond(503, {"status": "unavailable"})

    def log_message(self, format: str, *args: Any) -> None:
        print(
            f"HTTP {self.command} {urlparse(self.path).path} "
            f"{args[1] if len(args) > 1 else ''}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="explicitly create the deterministic smoke sentinels, then exit",
    )
    args = parser.parse_args()
    if args.seed:
        print(json.dumps(asyncio.run(_seed_once()), sort_keys=True))
    else:
        port = int(os.environ.get("PORT", "10000"))
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        print(f"AITOS PostgreSQL read-only persistence probe listening on port {port}")
        server.serve_forever()
