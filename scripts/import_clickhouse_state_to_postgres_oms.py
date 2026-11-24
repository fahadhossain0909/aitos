"""Lossless, resumable ClickHouse-to-PostgreSQL canonical OMS importer.

This is an explicit offline migration tool. It never drops or renames ClickHouse
tables. Run only after all legacy writers are stopped and a restorable backup
has been verified. Each source occurrence is retained verbatim in PostgreSQL
and represented by an idempotent canonical TRADE_SNAPSHOT event.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from aitos.execution.postgres_oms import PostgresCanonicalOMS
from scripts.migrate_scoped_clickhouse_state import (
    CLOSED_SOURCE,
    RUNTIME_SOURCE,
    _scope,
    _timestamp,
    source_fingerprint,
)


IMPORT_DDL = """
CREATE TABLE IF NOT EXISTS aitos_oms_legacy_imports (
    account_scope TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_fingerprint CHAR(64) NOT NULL,
    source_occurrence BIGINT NOT NULL CHECK (source_occurrence >= 0),
    trade_id TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    raw_payload TEXT NOT NULL,
    raw_payload_sha256 CHAR(64) NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_scope, source_table, source_fingerprint, source_occurrence)
)
"""


def _manifest_row(table: str, row: tuple[Any, ...], occurrence: int) -> dict[str, Any]:
    if table == RUNTIME_SOURCE:
        trade_id, symbol, state, raw, updated_at = row
    else:
        trade_id, raw, updated_at = row
        symbol = state = ""
    raw = str(raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("trade_id") != str(trade_id):
        raise ValueError(f"{table}: payload identity mismatch for {trade_id}")
    if table == RUNTIME_SOURCE and (
        str(payload.get("symbol") or "") != str(symbol)
        or str(payload.get("state") or "") != str(state)
    ):
        raise ValueError(f"{table}: row metadata differs from payload for {trade_id}")
    if table == CLOSED_SOURCE and payload.get("state") != "position_closed":
        raise ValueError(f"{table}: nonterminal row in closed ledger")
    fingerprint = source_fingerprint(table, str(trade_id), raw, updated_at)
    return {
        "source_table": table,
        "source_fingerprint": fingerprint,
        "source_occurrence": occurrence,
        "trade_id": str(trade_id),
        "symbol": str(symbol),
        "state": str(state),
        "updated_at": _timestamp(updated_at).isoformat(),
        "raw_payload": raw,
        "raw_payload_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


async def _import_one(
    pool: Any,
    oms: PostgresCanonicalOMS,
    fence: Any,
    item: dict[str, Any],
) -> None:
    scope = fence.account_scope
    payload = dict(item["payload"])
    embedded = payload.get("account_scope")
    if embedded and embedded != scope:
        raise ValueError("source payload scope conflicts with explicit attribution")
    payload["account_scope"] = scope
    trade_id = item["trade_id"]
    event_id = (
        f"legacy:{item['source_fingerprint']}:{item['source_occurrence']}"
    )
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            IMPORT_DDL
        )
        await conn.execute(
            """
            INSERT INTO aitos_oms_legacy_imports
                (account_scope,source_table,source_fingerprint,source_occurrence,
                 trade_id,source_updated_at,raw_payload,raw_payload_sha256)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT DO NOTHING
            """,
            scope,
            item["source_table"],
            item["source_fingerprint"],
            item["source_occurrence"],
            trade_id,
            item["updated_at"],
            item["raw_payload"],
            item["raw_payload_sha256"],
        )
        stored = await conn.fetchrow(
            """
            SELECT raw_payload_sha256,raw_payload FROM aitos_oms_legacy_imports
            WHERE account_scope=$1 AND source_table=$2 AND source_fingerprint=$3
              AND source_occurrence=$4
            """,
            scope,
            item["source_table"],
            item["source_fingerprint"],
            item["source_occurrence"],
        )
        if (
            stored is None
            or stored["raw_payload_sha256"] != item["raw_payload_sha256"]
            or stored["raw_payload"] != item["raw_payload"]
        ):
            raise RuntimeError("PostgreSQL import ledger payload parity mismatch")

    events = await oms.read_events(scope, trade_id)
    existing = next((event for event in events if event.event_id == event_id), None)
    if existing is not None:
        if existing.payload.get("trade") != payload:
            raise RuntimeError("canonical event ID exists with different payload")
        return
    await oms.append(
        fence,
        aggregate_id=trade_id,
        event_id=event_id,
        expected_revision=len(events),
        payload={
            "event_type": "TRADE_SNAPSHOT",
            "trade_id": trade_id,
            "trade": payload,
            "legacy_source": {
                "table": item["source_table"],
                "fingerprint": item["source_fingerprint"],
                "occurrence": item["source_occurrence"],
                "updated_at": item["updated_at"],
            },
        },
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if not (args.confirm_writers_stopped and args.confirm_backup_retained):
        raise RuntimeError(
            "offline import requires --confirm-writers-stopped and "
            "--confirm-backup-retained"
        )
    try:
        import asyncpg
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError("install asyncpg and clickhouse-connect") from exc
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL must target the PostgreSQL primary")
    ch = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    try:
        sources = {
            RUNTIME_SOURCE: list(ch.query(
                "SELECT trade_id,symbol,state,payload_json,updated_at "
                "FROM trade_runtime_state SETTINGS max_result_rows=250000, "
                "max_result_bytes=536870912, result_overflow_mode='throw', "
                "read_overflow_mode='throw'"
            ).result_rows),
            CLOSED_SOURCE: list(ch.query(
                "SELECT trade_id,payload_json,updated_at "
                "FROM paper_closed_trade_ledger SETTINGS max_result_rows=250000, "
                "max_result_bytes=536870912, result_overflow_mode='throw', "
                "read_overflow_mode='throw'"
            ).result_rows),
        }
        counts: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        for table, rows in sources.items():
            occurrences: Counter[str] = Counter()
            for row in rows:
                trade_id = str(row[0])
                raw = str(row[3] if table == RUNTIME_SOURCE else row[1])
                stamp = row[4] if table == RUNTIME_SOURCE else row[2]
                fp = source_fingerprint(table, trade_id, raw, stamp)
                occurrence = occurrences[fp]
                occurrences[fp] += 1
                item = _manifest_row(table, row, occurrence)
                mapped = args.scope_map.get(fp)
                embedded = item["payload"].get("account_scope")
                if embedded and mapped and embedded != mapped:
                    raise ValueError(f"scope map conflicts for {fp}")
                item["account_scope"] = _scope(embedded or mapped)
                counts[table] += 1
                items.append(item)
        args.manifest_out.write_text(
            json.dumps(
                [{k: v for k, v in item.items() if k != "payload"} for item in items],
                indent=2,
            ),
            encoding="utf-8",
        )
        by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_scope[item["account_scope"]].append(item)
        if not args.apply:
            return {
                "mode": "dry-run",
                "runtime_rows": counts[RUNTIME_SOURCE],
                "closed_rows": counts[CLOSED_SOURCE],
                "scopes": {scope: len(rows) for scope, rows in by_scope.items()},
                "manifest": str(args.manifest_out),
                "writes": 0,
            }
        await pool.execute(IMPORT_DDL)
        oms = PostgresCanonicalOMS(pool)
        imported = 0
        for scope, records in sorted(by_scope.items()):
            generation = await oms.writer_generation(scope)
            fence = await oms.acquire_writer(
                scope, f"clickhouse-import:{os.getpid()}",
                expected_generation=generation,
            )
            records.sort(
                key=lambda item: (
                    item["trade_id"],
                    item["updated_at"],
                    item["source_table"],
                    item["source_occurrence"],
                )
            )
            for item in records:
                await _import_one(pool, oms, fence, item)
                imported += 1
        expected_rows = counts[RUNTIME_SOURCE] + counts[CLOSED_SOURCE]
        if imported != expected_rows:
            raise RuntimeError(
                f"PostgreSQL import incomplete: expected {expected_rows}, imported {imported}"
            )
        return {
            "mode": "imported-and-verified",
            "runtime_rows": counts[RUNTIME_SOURCE],
            "closed_rows": counts[CLOSED_SOURCE],
            "imported_rows": imported,
            "postgres_import_rows_verified": imported,
            "manifest": str(args.manifest_out),
            "clickhouse_tables_mutated": False,
        }
    finally:
        await pool.close()
        ch.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    parser.add_argument("--confirm-backup-retained", action="store_true")
    parser.add_argument("--scope-map", type=Path, required=True)
    parser.add_argument(
        "--manifest-out", type=Path,
        default=Path("clickhouse-to-postgres-oms-manifest.json"),
    )
    args = parser.parse_args()
    mapping = json.loads(args.scope_map.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        parser.error("--scope-map must be a JSON object")
    args.scope_map = mapping
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
