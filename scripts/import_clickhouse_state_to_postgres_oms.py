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
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from aitos.execution.postgres_oms import PostgresCanonicalOMS
from aitos.storage.postgres_migrations import apply_postgres_migrations
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
    event_id = f"legacy:{item['source_fingerprint']}:{item['source_occurrence']}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(IMPORT_DDL)
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
            _timestamp(item["updated_at"]),
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
    if args.apply and not (
        args.confirm_writers_stopped and args.confirm_backup_retained
    ):
        raise RuntimeError(
            "apply requires --confirm-writers-stopped and "
            "--confirm-backup-retained"
        )
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError("install clickhouse-connect") from exc
    asyncpg = None
    if args.apply:
        try:
            import asyncpg as asyncpg_module
        except ImportError as exc:
            raise RuntimeError("install asyncpg for --apply") from exc
        asyncpg = asyncpg_module
    dsn = os.getenv("DATABASE_URL")
    if args.apply and not dsn:
        raise RuntimeError("DATABASE_URL must target the PostgreSQL primary")

    ch = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    pool = None
    manifest_path = Path(args.manifest_out)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    partial_manifest = manifest_path.with_suffix(
        manifest_path.suffix + ".partial"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="aitos-oms-import-") as workdir:
            stage = sqlite3.connect(Path(workdir) / "source-stage.sqlite")
            try:
                stage.execute(
                    """
                    CREATE TABLE staged_source (
                        account_scope TEXT NOT NULL,
                        source_table TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL,
                        source_occurrence INTEGER NOT NULL,
                        trade_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        raw_payload TEXT NOT NULL,
                        raw_payload_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (
                            account_scope, source_table,
                            source_fingerprint, source_occurrence
                        )
                    )
                    """
                )
                stage.execute(
                    "CREATE INDEX staged_trade_order ON staged_source "
                    "(account_scope,trade_id,updated_at,source_table)"
                )
                counts: Counter[str] = Counter()
                scope_counts: Counter[str] = Counter()
                queries = {
                    RUNTIME_SOURCE: (
                        "SELECT trade_id,symbol,state,payload_json,updated_at "
                        "FROM trade_runtime_state ORDER BY trade_id,updated_at,payload_json"
                    ),
                    CLOSED_SOURCE: (
                        "SELECT trade_id,payload_json,updated_at "
                        "FROM paper_closed_trade_ledger "
                        "ORDER BY trade_id,updated_at,payload_json"
                    ),
                }
                with partial_manifest.open("w", encoding="utf-8", newline="\\n") as manifest:
                    for table, query in queries.items():
                        occurrences: Counter[str] = Counter()
                        with ch.query_row_block_stream(query) as blocks:
                            for block in blocks:
                                for row in block:
                                    trade_id = str(row[0])
                                    raw = str(
                                        row[3] if table == RUNTIME_SOURCE else row[1]
                                    )
                                    stamp = row[4] if table == RUNTIME_SOURCE else row[2]
                                    fingerprint = source_fingerprint(
                                        table, trade_id, raw, stamp
                                    )
                                    occurrence = occurrences[fingerprint]
                                    occurrences[fingerprint] += 1
                                    item = _manifest_row(table, row, occurrence)
                                    mapped = args.scope_map.get(fingerprint)
                                    embedded = item["payload"].get("account_scope")
                                    if embedded and mapped and embedded != mapped:
                                        raise ValueError(
                                            f"scope map conflicts for {fingerprint}"
                                        )
                                    scope = _scope(embedded or mapped)
                                    item["account_scope"] = scope
                                    stage.execute(
                                        """
                                        INSERT INTO staged_source
                                            (account_scope,source_table,source_fingerprint,
                                             source_occurrence,trade_id,updated_at,
                                             raw_payload,raw_payload_sha256,payload_json)
                                        VALUES(?,?,?,?,?,?,?,?,?)
                                        """,
                                        (
                                            scope,
                                            table,
                                            fingerprint,
                                            occurrence,
                                            trade_id,
                                            item["updated_at"],
                                            item["raw_payload"],
                                            item["raw_payload_sha256"],
                                            json.dumps(
                                                item["payload"],
                                                separators=(",", ":"),
                                                ensure_ascii=False,
                                                allow_nan=False,
                                            ),
                                        ),
                                    )
                                    manifest.write(
                                        json.dumps(
                                            item,
                                            separators=(",", ":"),
                                            ensure_ascii=False,
                                            allow_nan=False,
                                        )
                                        + "\\n"
                                    )
                                    counts[table] += 1
                                    scope_counts[scope] += 1
                    manifest.flush()
                    os.fsync(manifest.fileno())
                stage.commit()

                ambiguous = stage.execute(
                    """
                    SELECT account_scope,trade_id,updated_at
                    FROM staged_source
                    GROUP BY account_scope,trade_id,updated_at
                    HAVING count(DISTINCT raw_payload_sha256) > 1
                    LIMIT 1
                    """
                ).fetchone()
                if ambiguous:
                    raise RuntimeError(
                        "ambiguous same-timestamp legacy snapshots for "
                        f"scope={ambiguous[0]} trade={ambiguous[1]}; refusing "
                        "to invent canonical event ordering"
                    )

                expected_rows = counts[RUNTIME_SOURCE] + counts[CLOSED_SOURCE]
                if not args.apply:
                    os.replace(partial_manifest, manifest_path)
                    return {
                        "mode": "dry-run",
                        "runtime_rows": counts[RUNTIME_SOURCE],
                        "closed_rows": counts[CLOSED_SOURCE],
                        "scopes": dict(scope_counts),
                        "manifest": str(manifest_path),
                        "writes": 0,
                        "postgres_connected": False,
                    }

                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
                await apply_postgres_migrations(pool)
                await pool.execute(IMPORT_DDL)
                oms = PostgresCanonicalOMS(pool)
                imported = 0
                canonical_verified = 0
                scopes = [
                    row[0]
                    for row in stage.execute(
                        "SELECT DISTINCT account_scope FROM staged_source "
                        "ORDER BY account_scope"
                    )
                ]
                for scope in scopes:
                    generation = await oms.writer_generation(scope)
                    fence = await oms.acquire_writer(
                        scope,
                        f"clickhouse-import:{os.getpid()}",
                        expected_generation=generation,
                    )
                    trade_ids = [
                        row[0]
                        for row in stage.execute(
                            "SELECT DISTINCT trade_id FROM staged_source "
                            "WHERE account_scope=? ORDER BY trade_id",
                            (scope,),
                        )
                    ]
                    for trade_id in trade_ids:
                        rows = stage.execute(
                            """
                            SELECT source_table,source_fingerprint,source_occurrence,
                                   updated_at,raw_payload,raw_payload_sha256,payload_json
                            FROM staged_source
                            WHERE account_scope=? AND trade_id=?
                            ORDER BY updated_at,
                                CASE WHEN source_table=? THEN 1 ELSE 0 END,
                                source_fingerprint,source_occurrence
                            """,
                            (scope, trade_id, CLOSED_SOURCE),
                        ).fetchall()
                        expected_events = {}
                        for row in rows:
                            item = {
                                "source_table": row[0],
                                "source_fingerprint": row[1],
                                "source_occurrence": int(row[2]),
                                "updated_at": row[3],
                                "raw_payload": row[4],
                                "raw_payload_sha256": row[5],
                                "trade_id": trade_id,
                                "payload": json.loads(row[6]),
                            }
                            await _import_one(pool, oms, fence, item)
                            imported += 1
                            event_id = (
                                f"legacy:{item['source_fingerprint']}:"
                                f"{item['source_occurrence']}"
                            )
                            expected_trade = dict(item["payload"])
                            expected_trade["account_scope"] = scope
                            expected_events[event_id] = expected_trade
                        events = await oms.read_events(scope, trade_id)
                        actual_events = {
                            event.event_id: event.payload.get("trade")
                            for event in events
                        }
                        for event_id, expected_trade in expected_events.items():
                            if actual_events.get(event_id) != expected_trade:
                                raise RuntimeError(
                                    "canonical OMS event parity failed for "
                                    f"scope={scope} trade={trade_id} event={event_id}"
                                )
                            canonical_verified += 1

                if imported != expected_rows or canonical_verified != expected_rows:
                    raise RuntimeError(
                        "PostgreSQL canonical import incomplete: "
                        f"source={expected_rows}, imported={imported}, "
                        f"verified={canonical_verified}"
                    )
                os.replace(partial_manifest, manifest_path)
                return {
                    "mode": "imported-and-verified",
                    "runtime_rows": counts[RUNTIME_SOURCE],
                    "closed_rows": counts[CLOSED_SOURCE],
                    "imported_rows": imported,
                    "canonical_events_verified": canonical_verified,
                    "manifest": str(manifest_path),
                    "clickhouse_tables_mutated": False,
                }
            finally:
                stage.close()
    finally:
        if pool is not None:
            await pool.close()
        ch.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    parser.add_argument("--confirm-backup-retained", action="store_true")
    parser.add_argument("--scope-map", type=Path, required=True)
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("clickhouse-to-postgres-oms-manifest.jsonl"),
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
