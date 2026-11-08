"""Durable decision-to-outcome attribution for entries, gates, HOLD and exits."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class DecisionOutcomeLedger:
    """Append-only decision journal with idempotent outcome reconciliation."""

    DECISION_TYPES = {
        "GATE",
        "ENTRY",
        "REJECTION",
        "HOLD",
        "MANAGE",
        "EXIT",
        "EXECUTION_FAILURE",
    }

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 10000):
        self.database_path = str(database_path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_outcomes (
                    decision_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    decision_type TEXT NOT NULL,
                    decision_at TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    outcome_value REAL,
                    outcome_at TEXT,
                    outcome_json TEXT,
                    candidate_id TEXT,
                    label_resolution_time TEXT,
                    outcome_provenance TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(decision_outcomes)")
            }
            for name in ("candidate_id", "label_resolution_time", "outcome_provenance"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE decision_outcomes ADD COLUMN {name} TEXT"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_candidate "
                "ON decision_outcomes(candidate_id, decision_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_entity "
                "ON decision_outcomes(entity_id, decision_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_cohort "
                "ON decision_outcomes(symbol, side, decision_type, decision_at)"
            )

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path, timeout=self.busy_timeout_ms / 1000
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def record_decision(
        self,
        decision_id: str,
        *,
        entity_id: str,
        symbol: str,
        side: str,
        decision_type: str,
        accepted: bool,
        reason: str,
        payload: Mapping[str, Any] | None = None,
        decision_at: str | None = None,
        candidate_id: str | None = None,
        outcome_provenance: str | None = None,
    ) -> bool:
        if not all(
            isinstance(v, str) and v.strip()
            for v in (decision_id, entity_id, symbol, side, decision_type)
        ):
            raise ValueError("decision identity fields are required")
        kind = decision_type.upper()
        if kind not in self.DECISION_TYPES:
            raise ValueError(f"unsupported decision_type: {decision_type}")
        timestamp = decision_at or datetime.now(timezone.utc).isoformat()
        body = json.dumps(dict(payload or {}), sort_keys=True, allow_nan=False)
        values = (
            entity_id,
            symbol.upper(),
            side.upper(),
            kind,
            timestamp,
            int(bool(accepted)),
            str(reason),
            body,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT entity_id,symbol,side,decision_type,decision_at,accepted,
                reason,payload_json,candidate_id,outcome_provenance
                FROM decision_outcomes WHERE decision_id=?""",
                (decision_id,),
            ).fetchone()
            values = values + (candidate_id, outcome_provenance)
            if row is not None:
                if tuple(row) != values:
                    connection.execute("ROLLBACK")
                    raise ValueError(
                        "decision_id already exists with different decision data"
                    )
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO decision_outcomes
                (decision_id,entity_id,symbol,side,decision_type,decision_at,accepted,
                 reason,payload_json,candidate_id,outcome_provenance)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, *values),
            )
            connection.execute("COMMIT")
            return True

    def resolve_entity(
        self,
        entity_id: str,
        *,
        outcome_value: float,
        outcome_at: str,
        outcome: Mapping[str, Any] | None = None,
        candidate_id: str | None = None,
        outcome_provenance: str | None = None,
    ) -> int:
        import math

        value = float(outcome_value)
        if not math.isfinite(value):
            raise ValueError("outcome_value must be finite")
        outcome_payload = dict(outcome or {})
        provenance = outcome_provenance or str(
            outcome_payload.get("provenance")
            or (
                "COUNTERFACTUAL_REPLAY"
                if outcome_payload.get("counterfactual")
                else "EXECUTED_TRADE"
            )
        )
        body = json.dumps(outcome_payload, sort_keys=True, allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT decision_id,outcome_value,outcome_at,outcome_json,
                candidate_id,label_resolution_time,outcome_provenance
                FROM decision_outcomes WHERE entity_id=?""",
                (entity_id,),
            ).fetchall()
            updated = 0
            for row in rows:
                if row["outcome_value"] is not None:
                    if (
                        row["outcome_value"],
                        row["outcome_at"],
                        row["outcome_json"],
                        row["candidate_id"],
                        row["label_resolution_time"],
                        row["outcome_provenance"],
                    ) != (
                        value,
                        outcome_at,
                        body,
                        candidate_id or row["candidate_id"],
                        outcome_at,
                        provenance,
                    ):
                        connection.execute("ROLLBACK")
                        raise ValueError(
                            "decision outcome already resolved differently"
                        )
                    continue
                connection.execute(
                    """UPDATE decision_outcomes SET outcome_value=?,outcome_at=?,
                    outcome_json=?,candidate_id=COALESCE(?,candidate_id),
                    label_resolution_time=?,outcome_provenance=?
                    WHERE decision_id=? AND outcome_value IS NULL""",
                    (
                        value,
                        outcome_at,
                        body,
                        candidate_id,
                        outcome_at,
                        provenance,
                        row["decision_id"],
                    ),
                )
                updated += 1
            connection.execute("COMMIT")
            return updated

    def list_decisions(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        decision_type: str | None = None,
        unresolved_only: bool = False,
        limit: int = 1000,
    ) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        normalized_symbol = symbol.upper() if symbol is not None else None
        normalized_type = decision_type.upper() if decision_type is not None else None
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM decision_outcomes
                WHERE (? IS NULL OR entity_id = ?)
                  AND (? IS NULL OR symbol = ?)
                  AND (? IS NULL OR decision_type = ?)
                  AND (? = 0 OR outcome_value IS NULL)
                ORDER BY decision_at ASC
                LIMIT ?
                """,
                (
                    entity_id,
                    entity_id,
                    normalized_symbol,
                    normalized_symbol,
                    normalized_type,
                    normalized_type,
                    int(unresolved_only),
                    limit,
                ),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "accepted": bool(row["accepted"]),
                "payload": json.loads(row["payload_json"]),
                "outcome": (
                    None
                    if row["outcome_json"] is None
                    else json.loads(row["outcome_json"])
                ),
            }
            for row in rows
        )


    def snapshot(self) -> dict[str, Any]:
        """Return bounded aggregate counts for runtime health and canary evidence."""
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS decisions_total,
                       SUM(CASE WHEN outcome_value IS NOT NULL THEN 1 ELSE 0 END)
                           AS resolved_total
                FROM decision_outcomes
                """
            ).fetchone()
            by_type = connection.execute(
                """
                SELECT decision_type, COUNT(*) AS count,
                       SUM(CASE WHEN outcome_value IS NOT NULL THEN 1 ELSE 0 END)
                           AS resolved
                FROM decision_outcomes
                GROUP BY decision_type ORDER BY decision_type
                """
            ).fetchall()
            by_provenance = connection.execute(
                """
                SELECT outcome_provenance, COUNT(*) AS count
                FROM decision_outcomes
                WHERE outcome_provenance IS NOT NULL
                GROUP BY outcome_provenance ORDER BY outcome_provenance
                """
            ).fetchall()
        decisions_total = int(totals["decisions_total"] or 0)
        resolved_total = int(totals["resolved_total"] or 0)
        return {
            "decisions_total": decisions_total,
            "resolved_total": resolved_total,
            "unresolved_total": decisions_total - resolved_total,
            "by_type": {
                str(row["decision_type"]): {
                    "count": int(row["count"]),
                    "resolved": int(row["resolved"] or 0),
                }
                for row in by_type
            },
            "by_provenance": {
                str(row["outcome_provenance"]): int(row["count"])
                for row in by_provenance
            },
        }
