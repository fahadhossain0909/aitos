"""Offline, read-only execution quarantine inventory.

Usage: python -m aitos.execution.recovery --database PATH --scope HASH
Never calls a venue, clears an intent, releases capital or asserts account safety.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def read_recovery_inventory(database: str | Path, scope: str) -> dict:
    path = Path(database).resolve()
    # URI read-only mode cannot silently create a missing journal.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT client_id,symbol,state,updated_at FROM execution_intents "
            "WHERE scope=? ORDER BY client_id",
            (scope,),
        ).fetchall()
    finally:
        conn.close()
    unresolved = [
        dict(row) for row in rows if row["state"] not in {"APPLIED", "REJECTED"}
    ]
    return {
        "evidence": "LOCAL_JOURNAL_ONLY",
        "scope_has_history": bool(rows),
        "venue_exposure_verified": False,
        "recovery_required": bool(unresolved),
        "unresolved_intents": unresolved,
    }


def read_protection_audit(database: str | Path, scope: str, evidence: dict) -> dict:
    from aitos.execution.protection_audit import audit_protection_inventory

    path = Path(database).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT scope,client_id,symbol,state,request_json,result_json "
            "FROM execution_intents WHERE scope=? ORDER BY client_id LIMIT 10001",
            (scope,),
        ).fetchall()
    finally:
        conn.close()
    return audit_protection_inventory(
        [dict(row) for row in rows], evidence, scope=scope
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument(
        "--protection-evidence",
        type=Path,
        help="Offline exported algo inventory JSON; never contacts an exchange",
    )
    args = parser.parse_args(argv)
    try:
        report = read_recovery_inventory(args.database, args.scope)
        if args.protection_evidence:
            if args.protection_evidence.stat().st_size > 5_000_000:
                raise ValueError("inventory too large")
            evidence = json.loads(args.protection_evidence.read_text())
            if not isinstance(evidence, dict):
                raise ValueError("inventory envelope required")
            report["protection_audit"] = read_protection_audit(
                args.database, args.scope, evidence
            )
    except (sqlite3.Error, OSError, ValueError):
        parser.exit(
            2, "Cannot read the execution journal; no recovery state was changed.\n"
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["recovery_required"] or "protection_audit" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
