"""Non-destructive, explicitly attributed ClickHouse state migration.

This tool stages legacy rows into scope-keyed append-only v2 tables. It never
mutates or drops legacy tables and deliberately does not switch runtime readers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNTIME_SOURCE = "trade_runtime_state"
CLOSED_SOURCE = "paper_closed_trade_ledger"
RUNTIME_TARGET = "trade_runtime_state_v2"
CLOSED_TARGET = "paper_closed_trade_ledger_v2"
CHUNK_SIZE = 500


def _ddl(table: str, *, runtime: bool) -> str:
    extra = """
    symbol LowCardinality(String),
    state LowCardinality(String),
""" if runtime else ""
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


def source_fingerprint(table: str, trade_id: str, payload_json: str, updated_at: Any) -> str:
    stamp = _timestamp(updated_at).isoformat(timespec="milliseconds")
    material = "\0".join((table, str(trade_id), stamp, payload_json))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _scope(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("each imported record requires an explicit valid account scope")
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
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=str,
    )
    payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    common = (
        account_scope, str(trade_id), _timestamp(updated_at), fingerprint,
        int(occurrence), canonical_payload, raw_payload, payload_hash,
    )
    if table == RUNTIME_SOURCE:
        record = (common[0], common[1], str(symbol), str(state), common[5],
                  common[6], common[2], common[3], common[4], common[7])
        return fingerprint, record
    record = (common[0], common[1], common[5], common[6], common[2],
              common[3], common[4], common[7])
    return fingerprint, record


def _read_sources(client: Any) -> dict[str, list[tuple[Any, ...]]]:
    runtime = client.query(
        "SELECT trade_id, symbol, state, payload_json, updated_at "
        "FROM trade_runtime_state"
    ).result_rows
    closed = client.query(
        "SELECT trade_id, payload_json, updated_at "
        "FROM paper_closed_trade_ledger"
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
            item = items.setdefault(fingerprint, {
                "source_table": table,
                "source_fingerprint": fingerprint,
                "trade_id": str(trade_id),
                "updated_at": _timestamp(stamp).isoformat(),
                "symbol": str(symbol),
                "state": str(state),
                "payload_sha256": hashlib.sha256(str(raw).encode("utf-8")).hexdigest(),
                "embedded_account_scope": payload.get("account_scope") if isinstance(payload, dict) else None,
                "occurrences": 0,
            })
            item["occurrences"] += 1
    return [items[key] for key in sorted(items)]


def _target_existing(client: Any, table: str, fingerprints: list[str]) -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()
    for start in range(0, len(fingerprints), CHUNK_SIZE):
        chunk = fingerprints[start:start + CHUNK_SIZE]
        if not chunk:
            continue
        result = client.query(
            f"SELECT source_fingerprint, source_occurrence FROM {table} "
            "WHERE source_fingerprint IN {fps:Array(String)}",
            parameters={"fps": chunk},
        )
        found.update((str(fp), int(occ)) for fp, occ in result.result_rows)
    return found


def _insert_table(client: Any, source: str, target: str, rows: list[tuple[Any, ...]],
                  scope_map: dict[str, str], *, runtime: bool) -> int:
    occurrences: Counter[str] = Counter()
    prepared: list[tuple[Any, ...]] = []
    for row in rows:
        trade_id = str(row[0])
        raw = str(row[3] if runtime else row[1])
        stamp = row[4] if runtime else row[2]
        fingerprint = source_fingerprint(source, trade_id, raw, stamp)
        occurrence = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        _, record = prepare_record(source, row, scope_map=scope_map, occurrence=occurrence)
        prepared.append(record)

    fingerprints = sorted(occurrences)
    existing = _target_existing(client, target, fingerprints)
    columns = (
        ["account_scope", "trade_id", "symbol", "state", "payload_json",
         "legacy_payload_json", "updated_at", "source_fingerprint",
         "source_occurrence", "payload_sha256"]
        if runtime else
        ["account_scope", "trade_id", "payload_json", "legacy_payload_json",
         "updated_at", "source_fingerprint", "source_occurrence", "payload_sha256"]
    )
    pending = []
    for record in prepared:
        fp_index = 7 if runtime else 5
        occ_index = 8 if runtime else 6
        identity = (str(record[fp_index]), int(record[occ_index]))
        if identity not in existing:
            pending.append(record)
            existing.add(identity)
    for start in range(0, len(pending), CHUNK_SIZE):
        client.insert(target, pending[start:start + CHUNK_SIZE], column_names=columns)

    expected = sum(occurrences.values())
    verified = client.query(
        f"SELECT count() FROM {target} WHERE source_fingerprint IN {{fps:Array(String)}}",
        parameters={"fps": fingerprints},
    ).result_rows[0][0] if fingerprints else 0
    if int(verified) < expected:
        raise RuntimeError(f"{target} verification incomplete: expected {expected}, observed {verified}")
    conflicts = client.query(
        f"SELECT count() FROM (SELECT source_fingerprint, source_occurrence "
        f"FROM {target} WHERE source_fingerprint IN {{fps:Array(String)}} "
        "GROUP BY source_fingerprint, source_occurrence HAVING count() != 1 OR uniqExact(payload_sha256) != 1)",
        parameters={"fps": fingerprints},
    ).result_rows[0][0] if fingerprints else 0
    if int(conflicts):
        raise RuntimeError(f"{target} contains conflicting imported evidence")
    return expected


def run(*, apply: bool, scope_map: dict[str, str], manifest_out: Path | None) -> dict[str, Any]:
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError("install clickhouse-connect to run this migration tool") from exc
    client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    try:
        sources = _read_sources(client)
        manifest = _manifest(sources)
        if manifest_out is not None:
            manifest_out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        missing = [
            item["source_fingerprint"] for item in manifest
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
                "message": "No writes performed. Assign every listed fingerprint an account scope, then rerun with --apply.",
            }
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
            client, RUNTIME_SOURCE, RUNTIME_TARGET, sources[RUNTIME_SOURCE],
            scope_map, runtime=True,
        )
        closed_count = _insert_table(
            client, CLOSED_SOURCE, CLOSED_TARGET, sources[CLOSED_SOURCE],
            scope_map, runtime=False,
        )
        return {
            "mode": "staged-and-verified",
            "runtime_rows": runtime_count,
            "closed_rows": closed_count,
            "legacy_tables_mutated": False,
            "runtime_cutover": False,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create/import into v2 tables; never alters legacy tables")
    parser.add_argument("--scope-map", type=Path, help="JSON object mapping source_fingerprint to explicit account_scope")
    parser.add_argument("--manifest-out", type=Path, help="write exact legacy-row attribution manifest")
    args = parser.parse_args()
    mapping = json.loads(args.scope_map.read_text(encoding="utf-8")) if args.scope_map else {}
    if not isinstance(mapping, dict):
        parser.error("--scope-map must contain a JSON object")
    result = run(apply=args.apply, scope_map=mapping, manifest_out=args.manifest_out)
    print(json.dumps(result, indent=2))
    return 0 if result.get("mode") != "manifest-only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
