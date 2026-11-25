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
    trade_id = item["trade_id"]
    event_id = f"legacy:{item['source_fingerprint']}:{item['source_occurrence']}"
    async with pool.acquire() as conn, conn.transaction():
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

    legacy_source = {
        "table": item["source_table"],
        "fingerprint": item["source_fingerprint"],
        "occurrence": item["source_occurrence"],
        "updated_at": item["updated_at"],
    }
    events = await oms.read_events(scope, trade_id)
    existing = next((event for event in events if event.event_id == event_id), None)
    if existing is None and any(
        not event.event_id.startswith("legacy:") for event in events
    ):
        raise RuntimeError(
            "legacy import conflicts with an existing canonical trade aggregate; "
            "manual reconciliation is required before merging histories"
        )
    if existing is not None:
        if (
            existing.payload.get("trade_id") != trade_id
            or existing.payload.get("legacy_source") != legacy_source
            or existing.payload.get("event_type") != "TRADE_SNAPSHOT"
        ):
            raise RuntimeError(
                "canonical event ID exists with different source evidence"
            )
        return
    await oms.append(
        fence,
        aggregate_id=trade_id,
        event_id=event_id,
        expected_revision=len(events),
        payload={
            "event_type": "TRADE_SNAPSHOT",
            "trade_id": trade_id,
            "legacy_source": legacy_source,
        },
    )


def _select_clickhouse_source(client: Any, source: str) -> tuple[str, bool]:
    """Prefer an already scope-keyed v2/active table and verify companion counts."""
    target = f"{source}_v2"
    backup = f"{source}_legacy_pre_scope_v1"
    rows = client.query(
        "SELECT table,name FROM system.columns "
        "WHERE database=currentDatabase() AND table IN "
        f"('{source}','{target}','{backup}')"
    ).result_rows
    columns: dict[str, set[str]] = defaultdict(set)
    for table_name, column_name in rows:
        columns[str(table_name)].add(str(column_name))
    candidates = [name for name in (source, target, backup) if name in columns]
    if not candidates:
        raise RuntimeError(f"ClickHouse source table {source} is missing")

    if "account_scope" in columns.get(source, set()):
        if backup not in columns:
            raise RuntimeError(
                f"scope-keyed active table {source} has no retained pre-scope backup"
            )
        selected, scoped = source, True
        companion = backup
    elif "account_scope" in columns.get(target, set()):
        companion = (
            source if source in columns else backup if backup in columns else None
        )
        if companion is None:
            raise RuntimeError(
                f"scope-keyed table {target} has no retained legacy source or backup"
            )
        selected, scoped = target, True
    elif source in columns:
        selected, scoped, companion = source, False, None
    elif "account_scope" in columns.get(backup, set()):
        selected, scoped, companion = backup, True, None
    else:
        raise RuntimeError(f"ClickHouse source table {source} has an unknown schema")

    if companion is not None:
        selected_count = int(
            client.query(f"SELECT count() FROM {selected} FINAL").result_rows[0][0]
        )
        companion_count = int(
            client.query(f"SELECT count() FROM {companion} FINAL").result_rows[0][0]
        )
        if selected_count != companion_count:
            raise RuntimeError(
                f"ClickHouse source/retained table count mismatch for {source}: "
                f"{selected_count} != {companion_count}"
            )
    return selected, scoped


def _scoped_payload_hash(payload: dict[str, Any], account_scope: str) -> str:
    canonical = dict(payload)
    embedded = canonical.get("account_scope")
    if embedded and embedded != account_scope:
        raise ValueError("scoped ClickHouse row conflicts with payload account_scope")
    canonical["account_scope"] = account_scope
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_reconciliation_report(path: Path, report: dict[str, Any]) -> None:
    """Persist a private, immutable reconciliation summary beside the manifest."""
    encoded = (
        json.dumps(
            report,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.apply and not (
        args.confirm_writers_stopped and args.confirm_backup_retained
    ):
        raise RuntimeError(
            "apply requires --confirm-writers-stopped and " "--confirm-backup-retained"
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

    manifest_path = Path(args.manifest_out)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    partial_manifest = manifest_path.with_suffix(manifest_path.suffix + ".partial")
    report_path = manifest_path.with_name(f"{manifest_path.stem}.reconciliation.json")
    quarantine_path = (
        Path(args.quarantine_out)
        if getattr(args, "quarantine_out", None) is not None
        else manifest_path.with_name(f"{manifest_path.stem}.quarantine.jsonl")
    )
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    if quarantine_path.resolve() in {
        manifest_path.resolve(),
        partial_manifest.resolve(),
        report_path.resolve(),
    }:
        raise ValueError("quarantine output must be distinct from migration evidence")
    if (
        manifest_path.exists()
        or manifest_path.is_symlink()
        or partial_manifest.exists()
        or partial_manifest.is_symlink()
        or report_path.exists()
        or report_path.is_symlink()
        or quarantine_path.exists()
        or quarantine_path.is_symlink()
    ):
        raise FileExistsError(
            "manifest or partial manifest already exists; choose a new path "
            "to preserve previous migration evidence"
        )
    ch = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    pool = None
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
                selected_sources = {
                    source: _select_clickhouse_source(ch, source)
                    for source in (RUNTIME_SOURCE, CLOSED_SOURCE)
                }
                queries = {}
                for source, (table_name, scoped) in selected_sources.items():
                    if scoped and source == RUNTIME_SOURCE:
                        query = (
                            "SELECT account_scope,trade_id,symbol,state,"
                            "legacy_payload_json,updated_at,source_fingerprint,"
                            "source_occurrence,payload_sha256 "
                            f"FROM {table_name} FINAL ORDER BY trade_id,updated_at,"
                            "source_fingerprint,source_occurrence "
                            "SETTINGS max_result_rows=0,max_result_bytes=0,"
                            "result_overflow_mode='throw',read_overflow_mode='throw'"
                        )
                    elif scoped:
                        query = (
                            "SELECT account_scope,trade_id,legacy_payload_json,"
                            "updated_at,source_fingerprint,source_occurrence,"
                            "payload_sha256 "
                            f"FROM {table_name} FINAL ORDER BY trade_id,updated_at,"
                            "source_fingerprint,source_occurrence "
                            "SETTINGS max_result_rows=0,max_result_bytes=0,"
                            "result_overflow_mode='throw',read_overflow_mode='throw'"
                        )
                    elif source == RUNTIME_SOURCE:
                        query = (
                            "SELECT trade_id,symbol,state,payload_json,updated_at "
                            f"FROM {table_name} ORDER BY trade_id,updated_at "
                            "SETTINGS max_result_rows=0,max_result_bytes=0,"
                            "result_overflow_mode='throw',read_overflow_mode='throw'"
                        )
                    else:
                        query = (
                            "SELECT trade_id,payload_json,updated_at "
                            f"FROM {table_name} ORDER BY trade_id,updated_at "
                            "SETTINGS max_result_rows=0,max_result_bytes=0,"
                            "result_overflow_mode='throw',read_overflow_mode='throw'"
                        )
                    queries[source] = (query, scoped)
                manifest_fd = os.open(
                    partial_manifest,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                quarantine_fd = os.open(
                    quarantine_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                quarantined_rows = 0
                source_rows_seen = 0
                with os.fdopen(
                    manifest_fd, "w", encoding="utf-8", newline="\n"
                ) as manifest, os.fdopen(
                    quarantine_fd, "w", encoding="utf-8", newline="\n"
                ) as quarantine:
                    for table, (query, scoped) in queries.items():
                        occurrences: Counter[str] = Counter()
                        with ch.query_row_block_stream(query) as blocks:
                            for block in blocks:
                                for row in block:
                                    source_rows_seen += 1
                                    if scoped and table == RUNTIME_SOURCE:
                                        (
                                            row_scope,
                                            trade_id,
                                            symbol,
                                            state,
                                            raw,
                                            stamp,
                                            stored_fingerprint,
                                            stored_occurrence,
                                            stored_payload_hash,
                                        ) = row
                                        legacy_row = (
                                            trade_id,
                                            symbol,
                                            state,
                                            raw,
                                            stamp,
                                        )
                                    elif scoped:
                                        (
                                            row_scope,
                                            trade_id,
                                            raw,
                                            stamp,
                                            stored_fingerprint,
                                            stored_occurrence,
                                            stored_payload_hash,
                                        ) = row
                                        legacy_row = (trade_id, raw, stamp)
                                    elif table == RUNTIME_SOURCE:
                                        trade_id = str(row[0])
                                        raw, stamp = str(row[3]), row[4]
                                        legacy_row = row
                                        row_scope = None
                                        stored_fingerprint = None
                                        stored_occurrence = None
                                        stored_payload_hash = None
                                    else:
                                        trade_id = str(row[0])
                                        raw, stamp = str(row[1]), row[2]
                                        legacy_row = row
                                        row_scope = None
                                        stored_fingerprint = None
                                        stored_occurrence = None
                                        stored_payload_hash = None

                                    trade_id = str(trade_id)
                                    fingerprint = ""
                                    occurrence = -1
                                    try:
                                        fingerprint = source_fingerprint(
                                            table, trade_id, str(raw), stamp
                                        )
                                        if scoped:
                                            occurrence = int(stored_occurrence)
                                            if fingerprint != str(stored_fingerprint):
                                                raise RuntimeError(
                                                    "scoped ClickHouse source fingerprint mismatch"
                                                )
                                        else:
                                            occurrence = occurrences[fingerprint]
                                            occurrences[fingerprint] += 1
                                        item = _manifest_row(
                                            table, legacy_row, occurrence
                                        )
                                        if scoped:
                                            scope = _scope(str(row_scope))
                                            expected_hash = _scoped_payload_hash(
                                                item["payload"], scope
                                            )
                                            if expected_hash != str(
                                                stored_payload_hash
                                            ):
                                                raise RuntimeError(
                                                    "scoped ClickHouse canonical payload hash mismatch"
                                                )
                                        else:
                                            mapped = args.scope_map.get(fingerprint)
                                            embedded = item["payload"].get(
                                                "account_scope"
                                            )
                                            if (
                                                embedded
                                                and mapped
                                                and embedded != mapped
                                            ):
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
                                    except (
                                        ValueError,
                                        TypeError,
                                        KeyError,
                                        RuntimeError,
                                        sqlite3.IntegrityError,
                                        IndexError,
                                    ) as exc:
                                        quarantine.write(
                                            json.dumps(
                                                {
                                                    "source_table": table,
                                                    "trade_id": trade_id,
                                                    "updated_at": str(stamp),
                                                    "source_fingerprint": fingerprint,
                                                    "source_occurrence": occurrence,
                                                    "reason": (
                                                        f"{type(exc).__name__}: {exc}"
                                                    ),
                                                    "raw_payload": str(raw),
                                                    "source_row": [
                                                        str(value) for value in row
                                                    ],
                                                },
                                                separators=(",", ":"),
                                                ensure_ascii=False,
                                                allow_nan=False,
                                            )
                                            + "\n"
                                        )
                                        quarantined_rows += 1
                                        continue
                                    manifest_item = {
                                        key: value
                                        for key, value in item.items()
                                        if key != "payload"
                                    }
                                    manifest.write(
                                        json.dumps(
                                            manifest_item,
                                            separators=(",", ":"),
                                            ensure_ascii=False,
                                            allow_nan=False,
                                        )
                                        + "\n"
                                    )
                                    counts[table] += 1
                                    scope_counts[scope] += 1
                        manifest.flush()
                    os.fsync(manifest.fileno())
                    quarantine.flush()
                    os.fsync(quarantine.fileno())
                stage.commit()

                ambiguous = stage.execute(
                    """
                    SELECT account_scope,trade_id,updated_at
                    FROM staged_source
                    GROUP BY account_scope,trade_id,updated_at
                    HAVING count(DISTINCT raw_payload_sha256) > 1
                    ORDER BY account_scope,trade_id,updated_at
                    """
                ).fetchall()
                ambiguous_rows_quarantined = 0
                for scope, trade_id, updated_at in ambiguous:
                    rows = stage.execute(
                        """
                        SELECT source_table,source_fingerprint,source_occurrence,
                               raw_payload,raw_payload_sha256
                        FROM staged_source
                        WHERE account_scope=? AND trade_id=? AND updated_at=?
                        ORDER BY source_table,source_fingerprint,source_occurrence
                        """,
                        (scope, trade_id, updated_at),
                    ).fetchall()
                    with quarantine_path.open("a", encoding="utf-8") as quarantine:
                        for row in rows:
                            quarantine.write(
                                json.dumps(
                                    {
                                        "account_scope": scope,
                                        "trade_id": trade_id,
                                        "updated_at": updated_at,
                                        "source_table": row[0],
                                        "source_fingerprint": row[1],
                                        "source_occurrence": int(row[2]),
                                        "raw_payload_sha256": row[4],
                                        "reason": "ambiguous_same_timestamp_conflicting_payload",
                                        "raw_payload": row[3],
                                    },
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                    allow_nan=False,
                                )
                                + "\n"
                            )
                            quarantined_rows += 1
                            ambiguous_rows_quarantined += 1
                if ambiguous_rows_quarantined:
                    with quarantine_path.open("a", encoding="utf-8") as quarantine:
                        quarantine.flush()
                        os.fsync(quarantine.fileno())

                expected_rows = counts[RUNTIME_SOURCE] + counts[CLOSED_SOURCE]
                row_errors_quarantined = quarantined_rows - ambiguous_rows_quarantined
                if quarantined_rows:
                    report = {
                        "mode": "quarantined",
                        "source_rows": source_rows_seen,
                        "accepted_source_rows": (
                            expected_rows - ambiguous_rows_quarantined
                        ),
                        "quarantined_rows": quarantined_rows,
                        "runtime_rows": counts[RUNTIME_SOURCE],
                        "closed_rows": counts[CLOSED_SOURCE],
                        "scope_counts": dict(scope_counts),
                        "source_fingerprints_verified": row_errors_quarantined == 0,
                        "occurrence_identity_preserved": True,
                        "ambiguous_attribution": bool(ambiguous),
                        "row_errors_quarantined": row_errors_quarantined,
                        "ambiguous_rows_quarantined": ambiguous_rows_quarantined,
                        "postgres_connected": False,
                        "writes": 0,
                        "quarantine_file": str(quarantine_path),
                        "import_blocked": True,
                    }
                    _write_reconciliation_report(report_path, report)
                    os.replace(partial_manifest, manifest_path)
                    return {
                        **report,
                        "manifest": str(manifest_path),
                        "reconciliation_report": str(report_path),
                    }
                quarantine_path.unlink()

                if not args.apply:
                    report = {
                        "mode": "dry-run",
                        "source_rows": expected_rows,
                        "runtime_rows": counts[RUNTIME_SOURCE],
                        "closed_rows": counts[CLOSED_SOURCE],
                        "scope_counts": dict(scope_counts),
                        "source_fingerprints_verified": True,
                        "occurrence_identity_preserved": True,
                        "ambiguous_attribution": False,
                        "postgres_connected": False,
                        "writes": 0,
                    }
                    _write_reconciliation_report(report_path, report)
                    os.replace(partial_manifest, manifest_path)
                    return {
                        **report,
                        "scopes": dict(scope_counts),
                        "manifest": str(manifest_path),
                        "reconciliation_report": str(report_path),
                    }

                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
                await apply_postgres_migrations(pool)
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
                scope_reconciliation = []
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
                            expected_events[event_id] = {
                                "table": item["source_table"],
                                "fingerprint": item["source_fingerprint"],
                                "occurrence": item["source_occurrence"],
                                "updated_at": item["updated_at"],
                            }
                        events = await oms.read_events(scope, trade_id)
                        actual_events = {
                            event.event_id: event.payload.get("legacy_source")
                            for event in events
                        }
                        expected_legacy_ids = set(expected_events)
                        actual_legacy_ids = {
                            event.event_id
                            for event in events
                            if event.event_id.startswith("legacy:")
                        }
                        if actual_legacy_ids != expected_legacy_ids:
                            missing = sorted(expected_legacy_ids - actual_legacy_ids)
                            unexpected = sorted(actual_legacy_ids - expected_legacy_ids)
                            raise RuntimeError(
                                "canonical legacy event set differs from source: "
                                f"scope={scope} trade={trade_id} "
                                f"missing={len(missing)} unexpected={len(unexpected)}"
                            )
                        for event_id, expected_source in expected_events.items():
                            if actual_events.get(event_id) != expected_source:
                                raise RuntimeError(
                                    "canonical OMS source-event parity failed for "
                                    f"scope={scope} trade={trade_id} event={event_id}"
                                )
                            canonical_verified += 1

                    expected_evidence = {
                        (
                            str(row[0]),
                            str(row[1]),
                            int(row[2]),
                            str(row[3]),
                            str(row[4]),
                            str(row[5]),
                        )
                        for row in stage.execute(
                            """
                            SELECT source_table,source_fingerprint,source_occurrence,
                                   trade_id,updated_at,raw_payload_sha256
                            FROM staged_source WHERE account_scope=?
                            """,
                            (scope,),
                        ).fetchall()
                    }
                    async with pool.acquire() as conn:
                        persisted_rows = await conn.fetch(
                            """
                            SELECT source_table,source_fingerprint,source_occurrence,
                                   trade_id,source_updated_at,raw_payload_sha256
                            FROM aitos_oms_legacy_imports
                            WHERE account_scope=$1
                            """,
                            scope,
                        )
                    actual_evidence = {
                        (
                            str(row["source_table"]),
                            str(row["source_fingerprint"]).strip(),
                            int(row["source_occurrence"]),
                            str(row["trade_id"]),
                            row["source_updated_at"].isoformat(),
                            str(row["raw_payload_sha256"]).strip(),
                        )
                        for row in persisted_rows
                    }
                    missing_evidence = expected_evidence - actual_evidence
                    unexpected_evidence = actual_evidence - expected_evidence
                    scope_report = {
                        "account_scope": scope,
                        "source_evidence_rows": len(expected_evidence),
                        "target_evidence_rows": len(actual_evidence),
                        "missing_evidence_rows": len(missing_evidence),
                        "unexpected_evidence_rows": len(unexpected_evidence),
                        "in_sync": not (missing_evidence or unexpected_evidence),
                    }
                    scope_reconciliation.append(scope_report)
                    if missing_evidence or unexpected_evidence:
                        raise RuntimeError(
                            "PostgreSQL legacy evidence differs from ClickHouse source: "
                            f"scope={scope}, missing={len(missing_evidence)}, "
                            f"unexpected={len(unexpected_evidence)}"
                        )

                if imported != expected_rows or canonical_verified != expected_rows:
                    raise RuntimeError(
                        "PostgreSQL canonical import incomplete: "
                        f"source={expected_rows}, imported={imported}, "
                        f"verified={canonical_verified}"
                    )
                report = {
                    "mode": "imported-and-verified",
                    "source_rows": expected_rows,
                    "runtime_rows": counts[RUNTIME_SOURCE],
                    "closed_rows": counts[CLOSED_SOURCE],
                    "imported_rows": imported,
                    "canonical_events_verified": canonical_verified,
                    "scope_reconciliation": scope_reconciliation,
                    "source_fingerprints_verified": True,
                    "occurrence_identity_preserved": True,
                    "ambiguous_attribution": False,
                    "clickhouse_tables_mutated": False,
                }
                _write_reconciliation_report(report_path, report)
                os.replace(partial_manifest, manifest_path)
                return {
                    **report,
                    "manifest": str(manifest_path),
                    "reconciliation_report": str(report_path),
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
    parser.add_argument("--scope-map", type=Path)
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("clickhouse-to-postgres-oms-manifest.jsonl"),
    )
    parser.add_argument(
        "--quarantine-out",
        type=Path,
        help="private JSONL file for source rows rejected from migration",
    )
    args = parser.parse_args()
    mapping = (
        json.loads(args.scope_map.read_text(encoding="utf-8"))
        if args.scope_map is not None
        else {}
    )
    if not isinstance(mapping, dict):
        parser.error("--scope-map must be a JSON object")
    args.scope_map = mapping
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    return 2 if result.get("import_blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
