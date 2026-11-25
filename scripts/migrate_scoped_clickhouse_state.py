"""Non-destructive, explicitly attributed ClickHouse state migration.

This tool stages legacy rows into scope-keyed append-only v2 tables. It never
mutates or drops legacy tables and deliberately does not switch runtime readers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_SOURCE = "trade_runtime_state"
CLOSED_SOURCE = "paper_closed_trade_ledger"
RUNTIME_TARGET = "trade_runtime_state_v2"
CLOSED_TARGET = "paper_closed_trade_ledger_v2"
RUNTIME_BACKUP = "trade_runtime_state_legacy_pre_scope_v1"
CLOSED_BACKUP = "paper_closed_trade_ledger_legacy_pre_scope_v1"
CHUNK_SIZE = 500


def _ddl(table: str, *, runtime: bool) -> str:
    extra = (
        """
    symbol LowCardinality(String),
    state LowCardinality(String),
"""
        if runtime
        else ""
    )
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    account_scope String,
    trade_id String,
{extra}    payload_json String,
    legacy_payload_json String,
    updated_at DateTime64(3, 'UTC'),
    source_fingerprint FixedString(64),
    source_occurrence UInt64,
    payload_sha256 FixedString(64)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(updated_at)
ORDER BY (account_scope, trade_id, updated_at, source_fingerprint, source_occurrence)
"""


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def source_fingerprint(
    table: str, trade_id: str, payload_json: str, updated_at: Any
) -> str:
    stamp = _timestamp(updated_at).isoformat(timespec="milliseconds")
    material = "\0".join((table, str(trade_id), stamp, payload_json))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _scope(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 256 or "\x00" in value:
        raise ValueError(
            "each imported record requires an explicit valid account scope"
        )
    return value


def prepare_record(
    table: str,
    row: tuple[Any, ...],
    *,
    scope_map: dict[str, str],
    occurrence: int,
) -> tuple[str, tuple[Any, ...]]:
    if table == RUNTIME_SOURCE:
        trade_id, symbol, state, raw_payload, updated_at = row
    elif table == CLOSED_SOURCE:
        trade_id, raw_payload, updated_at = row
        symbol = ""
        state = ""
    else:
        raise ValueError("unsupported legacy source table")

    raw_payload = str(raw_payload)
    fingerprint = source_fingerprint(table, str(trade_id), raw_payload, updated_at)
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict) or payload.get("trade_id") != str(trade_id):
        raise ValueError("legacy payload identity mismatch")
    embedded_scope = payload.get("account_scope")
    mapped_scope = scope_map.get(fingerprint)
    if embedded_scope and mapped_scope and embedded_scope != mapped_scope:
        raise ValueError("scope map conflicts with payload account_scope")
    account_scope = _scope(embedded_scope or mapped_scope)
    if table == RUNTIME_SOURCE:
        if str(payload.get("symbol") or "") != str(symbol):
            raise ValueError("runtime symbol does not match payload")
        if str(payload.get("state") or "") != str(state):
            raise ValueError("runtime state does not match payload")
    else:
        if payload.get("state") != "position_closed":
            raise ValueError("closed ledger contains a nonterminal payload")

    payload["account_scope"] = account_scope
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    common = (
        account_scope,
        str(trade_id),
        _timestamp(updated_at),
        fingerprint,
        int(occurrence),
        canonical_payload,
        raw_payload,
        payload_hash,
    )
    if table == RUNTIME_SOURCE:
        record = (
            common[0],
            common[1],
            str(symbol),
            str(state),
            common[5],
            common[6],
            common[2],
            common[3],
            common[4],
            common[7],
        )
        return fingerprint, record
    record = (
        common[0],
        common[1],
        common[5],
        common[6],
        common[2],
        common[3],
        common[4],
        common[7],
    )
    return fingerprint, record


def _read_sources(client: Any) -> dict[str, list[tuple[Any, ...]]]:
    runtime = client.query(
        "SELECT trade_id, symbol, state, payload_json, updated_at "
        "FROM trade_runtime_state "
        "SETTINGS max_result_rows=250000, max_result_bytes=536870912, "
        "result_overflow_mode='throw', read_overflow_mode='throw'"
    ).result_rows
    closed = client.query(
        "SELECT trade_id, payload_json, updated_at "
        "FROM paper_closed_trade_ledger "
        "SETTINGS max_result_rows=250000, max_result_bytes=536870912, "
        "result_overflow_mode='throw', read_overflow_mode='throw'"
    ).result_rows
    return {RUNTIME_SOURCE: list(runtime), CLOSED_SOURCE: list(closed)}


def _manifest(sources: dict[str, list[tuple[Any, ...]]]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for table, rows in sources.items():
        for row in rows:
            if table == RUNTIME_SOURCE:
                trade_id, symbol, state, raw, stamp = row
            else:
                trade_id, raw, stamp = row
                symbol = state = ""
            fingerprint = source_fingerprint(table, str(trade_id), str(raw), stamp)
            payload = json.loads(str(raw))
            item = items.setdefault(
                fingerprint,
                {
                    "source_table": table,
                    "source_fingerprint": fingerprint,
                    "trade_id": str(trade_id),
                    "updated_at": _timestamp(stamp).isoformat(),
                    "symbol": str(symbol),
                    "state": str(state),
                    "payload_sha256": hashlib.sha256(
                        str(raw).encode("utf-8")
                    ).hexdigest(),
                    "embedded_account_scope": (
                        payload.get("account_scope")
                        if isinstance(payload, dict)
                        else None
                    ),
                    "occurrences": 0,
                },
            )
            item["occurrences"] += 1
    return [items[key] for key in sorted(items)]


def _target_existing(
    client: Any, table: str, fingerprints: list[str]
) -> dict[tuple[str, int], str]:
    found: dict[tuple[str, int], str] = {}
    for start in range(0, len(fingerprints), CHUNK_SIZE):
        chunk = fingerprints[start : start + CHUNK_SIZE]
        if not chunk:
            continue
        result = client.query(
            f"SELECT source_fingerprint, source_occurrence, "
            f"any(payload_sha256), uniqExact(payload_sha256) FROM {table} "
            "WHERE source_fingerprint IN {fps:Array(String)} "
            "GROUP BY source_fingerprint, source_occurrence",
            parameters={"fps": chunk},
        )
        for fingerprint, occurrence, payload_hash, versions in result.result_rows:
            if int(versions) != 1:
                raise RuntimeError(
                    "target contains conflicting source occurrence evidence"
                )
            found[(str(fingerprint), int(occurrence))] = str(payload_hash)
    return found


def _insert_table(
    client: Any,
    source: str,
    target: str,
    rows: list[tuple[Any, ...]],
    scope_map: dict[str, str],
    *,
    runtime: bool,
) -> int:
    occurrences: Counter[str] = Counter()
    prepared: list[tuple[Any, ...]] = []
    for row in rows:
        trade_id = str(row[0])
        raw = str(row[3] if runtime else row[1])
        stamp = row[4] if runtime else row[2]
        fingerprint = source_fingerprint(source, trade_id, raw, stamp)
        occurrence = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        _, record = prepare_record(
            source, row, scope_map=scope_map, occurrence=occurrence
        )
        prepared.append(record)

    fingerprints = sorted(occurrences)
    existing = _target_existing(client, target, fingerprints)
    columns = (
        [
            "account_scope",
            "trade_id",
            "symbol",
            "state",
            "payload_json",
            "legacy_payload_json",
            "updated_at",
            "source_fingerprint",
            "source_occurrence",
            "payload_sha256",
        ]
        if runtime
        else [
            "account_scope",
            "trade_id",
            "payload_json",
            "legacy_payload_json",
            "updated_at",
            "source_fingerprint",
            "source_occurrence",
            "payload_sha256",
        ]
    )
    pending = []
    for record in prepared:
        fp_index = 7 if runtime else 5
        occ_index = 8 if runtime else 6
        identity = (str(record[fp_index]), int(record[occ_index]))
        hash_index = 9 if runtime else 7
        expected_hash = str(record[hash_index])
        if identity in existing:
            if existing[identity] != expected_hash:
                raise RuntimeError("target source occurrence payload hash mismatch")
        else:
            pending.append(record)
            existing[identity] = expected_hash
    for start in range(0, len(pending), CHUNK_SIZE):
        client.insert(target, pending[start : start + CHUNK_SIZE], column_names=columns)

    expected = sum(occurrences.values())
    verified = (
        client.query(
            f"SELECT count() FROM {target} "
            "WHERE source_fingerprint IN {fps:Array(String)}",
            parameters={"fps": fingerprints},
        ).result_rows[0][0]
        if fingerprints
        else 0
    )
    if int(verified) != expected:
        raise RuntimeError(
            f"{target} verification incomplete: expected {expected}, "
            f"observed {verified}"
        )
    conflicts = (
        client.query(
            f"SELECT count() FROM (SELECT source_fingerprint, source_occurrence "
            f"FROM {target} WHERE source_fingerprint IN {{fps:Array(String)}} "
            "GROUP BY source_fingerprint, source_occurrence "
            "HAVING count() != 1 OR uniqExact(payload_sha256) != 1)",
            parameters={"fps": fingerprints},
        ).result_rows[0][0]
        if fingerprints
        else 0
    )
    if int(conflicts):
        raise RuntimeError(f"{target} contains conflicting imported evidence")
    return expected


def _source_occurrence_records(
    client: Any,
    table: str,
    source: str,
    *,
    runtime: bool,
    scope_keyed: bool,
) -> Counter[tuple[Any, ...]]:
    """Read physical occurrences and verify every scoped row's raw evidence."""
    records: Counter[tuple[Any, ...]] = Counter()
    occurrences: Counter[str] = Counter()
    settings = (
        " SETTINGS max_result_rows=250000,max_result_bytes=536870912,"
        "result_overflow_mode='throw',read_overflow_mode='throw'"
    )
    if scope_keyed:
        if runtime:
            query = (
                "SELECT account_scope,trade_id,symbol,state,payload_json,"
                "legacy_payload_json,updated_at,source_fingerprint,"
                "source_occurrence,payload_sha256 "
                f"FROM {table} ORDER BY trade_id,updated_at,"
                "source_fingerprint,source_occurrence" + settings
            )
        else:
            query = (
                "SELECT account_scope,trade_id,payload_json,legacy_payload_json,"
                "updated_at,source_fingerprint,source_occurrence,payload_sha256 "
                f"FROM {table} ORDER BY trade_id,updated_at,"
                "source_fingerprint,source_occurrence" + settings
            )
    elif runtime:
        query = (
            "SELECT trade_id,symbol,state,payload_json,updated_at "
            f"FROM {table} ORDER BY trade_id,updated_at" + settings
        )
    else:
        query = (
            "SELECT trade_id,payload_json,updated_at "
            f"FROM {table} ORDER BY trade_id,updated_at" + settings
        )

    for row in client.query(query).result_rows:
        if scope_keyed and runtime:
            scope, trade_id, symbol, state, encoded, raw, stamp, stored_fp, occurrence, stored_hash = row
        elif scope_keyed:
            scope, trade_id, encoded, raw, stamp, stored_fp, occurrence, stored_hash = row
            symbol = state = ""
        elif runtime:
            trade_id, symbol, state, raw, stamp = row
            encoded = stored_fp = stored_hash = scope = None
            occurrence = None
        else:
            trade_id, raw, stamp = row
            symbol = state = encoded = stored_fp = stored_hash = scope = None
            occurrence = None

        trade_id = str(trade_id)
        raw = str(raw)
        stamp_key = _timestamp(stamp).isoformat(timespec="milliseconds")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{table} contains invalid legacy payload") from exc
        if not isinstance(payload, dict) or payload.get("trade_id") != trade_id:
            raise RuntimeError(f"{table} legacy payload identity mismatch")
        if runtime:
            symbol, state = str(symbol), str(state)
            if payload.get("symbol") != symbol or payload.get("state") != state:
                raise RuntimeError(f"{table} legacy metadata mismatch")
        elif payload.get("state") != "position_closed":
            raise RuntimeError(f"{table} contains a nonterminal record")

        fingerprint = source_fingerprint(source, trade_id, raw, stamp)
        if scope_keyed:
            scope = _scope(str(scope))
            if str(stored_fp) != fingerprint:
                raise RuntimeError(f"{table} source fingerprint mismatch")
            occurrence = int(occurrence)
            if occurrence < 0:
                raise RuntimeError(f"{table} has a negative source occurrence")
            canonical = dict(payload)
            embedded = canonical.get("account_scope")
            if embedded and embedded != scope:
                raise RuntimeError(f"{table} payload scope mismatch")
            canonical["account_scope"] = scope
            canonical_json = json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
                default=str,
            )
            digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
            if str(encoded) != canonical_json or str(stored_hash) != digest:
                raise RuntimeError(f"{table} canonical payload hash mismatch")
        else:
            occurrence = occurrences[fingerprint]
            occurrences[fingerprint] += 1

        identity = (
            fingerprint,
            int(occurrence),
            trade_id,
            symbol,
            state,
            stamp_key,
            raw,
        )
        records[identity] += 1
    return records


def _verify_scoped_matches_legacy(
    client: Any,
    *,
    scoped_table: str,
    legacy_table: str,
    source: str,
    runtime: bool,
) -> int:
    scoped = _source_occurrence_records(
        client, scoped_table, source, runtime=runtime, scope_keyed=True
    )
    legacy = _source_occurrence_records(
        client, legacy_table, source, runtime=runtime, scope_keyed=False
    )
    if scoped != legacy:
        raise RuntimeError(
            f"{source} rollback parity failed: scoped table differs from retained legacy backup"
        )
    return sum(legacy.values())


def _verified_cutover(client: Any) -> None:
    """Atomically replace legacy table names after import/parity verification.

    The database must be Atomic and writers must be externally quiesced. The
    old tables are retained under explicit backup names; this function never
    drops or truncates data.
    """
    engine = client.query(
        "SELECT engine FROM system.databases WHERE name=currentDatabase()"
    ).result_rows
    if not engine or str(engine[0][0]).lower() != "atomic":
        raise RuntimeError("verified cutover requires an Atomic ClickHouse database")
    existing = {
        str(row[0])
        for row in client.query(
            "SELECT name FROM system.tables WHERE database=currentDatabase()"
        ).result_rows
    }
    for name in (RUNTIME_BACKUP, CLOSED_BACKUP):
        if name in existing:
            raise RuntimeError(
                f"refusing cutover: retained backup already exists: {name}"
            )
    for source, target in (
        (RUNTIME_SOURCE, RUNTIME_TARGET),
        (CLOSED_SOURCE, CLOSED_TARGET),
    ):
        source_count = int(
            client.query(f"SELECT count() FROM {source}").result_rows[0][0]
        )
        target_count = int(
            client.query(f"SELECT count() FROM {target}").result_rows[0][0]
        )
        if source_count != target_count:
            raise RuntimeError(
                f"refusing cutover: {source}={source_count}, {target}={target_count}"
            )
    client.command(
        f"RENAME TABLE {RUNTIME_SOURCE} TO {RUNTIME_BACKUP}, "
        f"{RUNTIME_TARGET} TO {RUNTIME_SOURCE}, "
        f"{CLOSED_SOURCE} TO {CLOSED_BACKUP}, "
        f"{CLOSED_TARGET} TO {CLOSED_SOURCE}"
    )
    for active, backup in (
        (RUNTIME_SOURCE, RUNTIME_BACKUP),
        (CLOSED_SOURCE, CLOSED_BACKUP),
    ):
        active_count = int(
            client.query(f"SELECT count() FROM {active}").result_rows[0][0]
        )
        backup_count = int(
            client.query(f"SELECT count() FROM {backup}").result_rows[0][0]
        )
        if active_count != backup_count:
            raise RuntimeError(
                f"post-cutover row-count mismatch: {active}={active_count}, {backup}={backup_count}"
            )



def _verified_rollback(
    client: Any, *, writers_quiesced: bool, backup_confirmed: bool
) -> dict[str, Any]:
    """Restore legacy table names only when no post-cutover data has diverged.

    The scope-keyed tables are retained under their v2 names. A rollback is
    refused if writers are not quiesced, either backup is absent, a v2 name is
    already occupied, or the full physical source occurrence sets differ.
    """
    if not (writers_quiesced and backup_confirmed):
        raise RuntimeError(
            "rollback requires explicit writer-stop and restorable-backup confirmations"
        )
    engine = client.query(
        "SELECT engine FROM system.databases WHERE name=currentDatabase()"
    ).result_rows
    if not engine or str(engine[0][0]).lower() != "atomic":
        raise RuntimeError("verified rollback requires an Atomic ClickHouse database")
    existing = {
        str(row[0])
        for row in client.query(
            "SELECT name FROM system.tables WHERE database=currentDatabase()"
        ).result_rows
    }
    required = {RUNTIME_SOURCE, CLOSED_SOURCE, RUNTIME_BACKUP, CLOSED_BACKUP}
    if not required.issubset(existing):
        raise RuntimeError("rollback requires both active scope-keyed tables and backups")
    if RUNTIME_TARGET in existing or CLOSED_TARGET in existing:
        raise RuntimeError("rollback refuses to overwrite existing v2 table names")

    runtime_count = _verify_scoped_matches_legacy(
        client,
        scoped_table=RUNTIME_SOURCE,
        legacy_table=RUNTIME_BACKUP,
        source=RUNTIME_SOURCE,
        runtime=True,
    )
    closed_count = _verify_scoped_matches_legacy(
        client,
        scoped_table=CLOSED_SOURCE,
        legacy_table=CLOSED_BACKUP,
        source=CLOSED_SOURCE,
        runtime=False,
    )
    client.command(
        f"RENAME TABLE {RUNTIME_SOURCE} TO {RUNTIME_TARGET}, "
        f"{RUNTIME_BACKUP} TO {RUNTIME_SOURCE}, "
        f"{CLOSED_SOURCE} TO {CLOSED_TARGET}, "
        f"{CLOSED_BACKUP} TO {CLOSED_SOURCE}"
    )
    try:
        _verify_scoped_matches_legacy(
            client,
            scoped_table=RUNTIME_TARGET,
            legacy_table=RUNTIME_SOURCE,
            source=RUNTIME_SOURCE,
            runtime=True,
        )
        _verify_scoped_matches_legacy(
            client,
            scoped_table=CLOSED_TARGET,
            legacy_table=CLOSED_SOURCE,
            source=CLOSED_SOURCE,
            runtime=False,
        )
    except Exception as exc:
        # Re-enter the previous cutover layout; never leave a half-verified rollback.
        client.command(
            f"RENAME TABLE {RUNTIME_SOURCE} TO {RUNTIME_BACKUP}, "
            f"{RUNTIME_TARGET} TO {RUNTIME_SOURCE}, "
            f"{CLOSED_SOURCE} TO {CLOSED_BACKUP}, "
            f"{CLOSED_TARGET} TO {CLOSED_SOURCE}"
        )
        raise RuntimeError(
            "rollback postcondition failed; restored the previous cutover layout"
        ) from exc
    return {
        "mode": "rollback-and-verified",
        "runtime_rows": runtime_count,
        "closed_rows": closed_count,
        "legacy_tables_restored": True,
        "scoped_tables_retained": [RUNTIME_TARGET, CLOSED_TARGET],
    }

def run(
    *,
    apply: bool,
    scope_map: dict[str, str],
    manifest_out: Path | None,
    cutover: bool = False,
    writers_quiesced: bool = False,
    backup_confirmed: bool = False,
    rollback: bool = False,
) -> dict[str, Any]:
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError(
            "install clickhouse-connect to run this migration tool"
        ) from exc
    client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    try:
        if rollback:
            if cutover or apply:
                raise ValueError("--rollback cannot be combined with --apply or --cutover")
            return _verified_rollback(
                client,
                writers_quiesced=writers_quiesced,
                backup_confirmed=backup_confirmed,
            )
        sources = _read_sources(client)
        manifest = _manifest(sources)
        if manifest_out is not None:
            manifest_out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        missing = [
            item["source_fingerprint"]
            for item in manifest
            if not item["embedded_account_scope"]
            and item["source_fingerprint"] not in scope_map
        ]
        if missing:
            return {
                "mode": "manifest-only",
                "runtime_rows": len(sources[RUNTIME_SOURCE]),
                "closed_rows": len(sources[CLOSED_SOURCE]),
                "unattributed_records": len(missing),
                "manifest": str(manifest_out) if manifest_out else None,
                "message": (
                    "No writes performed. Assign every listed fingerprint an "
                    "account scope, then rerun with --apply."
                ),
            }
        if cutover and not apply:
            raise ValueError("--cutover requires --apply")
        if cutover and not (writers_quiesced and backup_confirmed):
            raise RuntimeError(
                "cutover requires explicit writer-stop and restorable-backup confirmations"
            )
        if not apply:
            return {
                "mode": "dry-run",
                "runtime_rows": len(sources[RUNTIME_SOURCE]),
                "closed_rows": len(sources[CLOSED_SOURCE]),
                "unattributed_records": 0,
                "message": "No writes performed. Review attribution and rerun with --apply.",
            }

        client.command(_ddl(RUNTIME_TARGET, runtime=True))
        client.command(_ddl(CLOSED_TARGET, runtime=False))
        runtime_count = _insert_table(
            client,
            RUNTIME_SOURCE,
            RUNTIME_TARGET,
            sources[RUNTIME_SOURCE],
            scope_map,
            runtime=True,
        )
        closed_count = _insert_table(
            client,
            CLOSED_SOURCE,
            CLOSED_TARGET,
            sources[CLOSED_SOURCE],
            scope_map,
            runtime=False,
        )
        if cutover:
            _verified_cutover(client)
        return {
            "mode": "cutover-and-verified" if cutover else "staged-and-verified",
            "runtime_rows": runtime_count,
            "closed_rows": closed_count,
            "legacy_tables_mutated": cutover,
            "legacy_backups": [RUNTIME_BACKUP, CLOSED_BACKUP] if cutover else [],
            "runtime_cutover": cutover,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create/import into v2 tables; legacy tables are retained",
    )
    parser.add_argument(
        "--cutover",
        action="store_true",
        help="after verified import, atomically rename v2 tables into runtime names; requires frozen writers and explicit acknowledgement",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="restore legacy runtime table names after exact parity and explicit freeze/backup confirmation",
    )
    parser.add_argument(
        "--scope-map",
        type=Path,
        help="JSON object mapping source_fingerprint to explicit account_scope",
    )
    parser.add_argument(
        "--confirm-writers-stopped",
        action="store_true",
        help="operator attests both legacy writers are stopped",
    )
    parser.add_argument(
        "--confirm-backup-retained",
        action="store_true",
        help="operator attests a restorable source backup exists",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("scoped-state-attribution-manifest.json"),
        help="write exact legacy-row attribution manifest",
    )
    args = parser.parse_args()
    if args.apply and not (
        args.confirm_writers_stopped and args.confirm_backup_retained
    ):
        parser.error(
            "--apply requires --confirm-writers-stopped and "
            "--confirm-backup-retained"
        )
    if args.rollback and not (
        args.confirm_writers_stopped and args.confirm_backup_retained
    ):
        parser.error(
            "--rollback requires --confirm-writers-stopped and "
            "--confirm-backup-retained"
        )
    mapping = (
        json.loads(args.scope_map.read_text(encoding="utf-8")) if args.scope_map else {}
    )
    if not isinstance(mapping, dict):
        parser.error("--scope-map must contain a JSON object")
    result = run(
        apply=args.apply,
        scope_map=mapping,
        manifest_out=args.manifest_out,
        cutover=args.cutover,
        writers_quiesced=args.confirm_writers_stopped,
        backup_confirmed=args.confirm_backup_retained,
        rollback=args.rollback,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("mode") != "manifest-only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
