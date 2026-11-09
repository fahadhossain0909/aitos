"""Durable artifact registry with explicit champion promotion and rollback."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .governance import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)


@dataclass(frozen=True)
class ModelArtifact:
    model_id: str
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    config_hash: str
    feature_schema_version: str
    status: str
    created_at: str
    metadata: Mapping[str, Any]


class StatisticalModelRegistry:
    """SQLite-backed registry; it stores references, never loads model code."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS statistical_model_artifacts (
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    artifact_uri TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    feature_schema_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('CHALLENGER', 'CHAMPION', 'ARCHIVED')
                    ),
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (model_id, model_version)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_champion
                ON statistical_model_artifacts(model_id)
                WHERE status='CHAMPION'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS statistical_model_promotion_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _timestamp(value: str | None) -> str:
        current = value or datetime.now(timezone.utc).isoformat()
        try:
            parsed = datetime.fromisoformat(current.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _artifact(row: sqlite3.Row) -> ModelArtifact:
        return ModelArtifact(
            model_id=row["model_id"],
            model_version=row["model_version"],
            artifact_uri=row["artifact_uri"],
            artifact_sha256=row["artifact_sha256"],
            config_hash=row["config_hash"],
            feature_schema_version=row["feature_schema_version"],
            status=row["status"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    def register_candidate(
        self,
        *,
        model_id: str,
        model_version: str,
        artifact_uri: str,
        artifact_sha256: str,
        config_hash: str,
        feature_schema_version: str,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> bool:
        """Register an immutable challenger; identical retries are idempotent."""
        required = (
            model_id,
            model_version,
            artifact_uri,
            config_hash,
            feature_schema_version,
        )
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError("model artifact identity and hashes are required")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact_sha256):
            raise ValueError(
                "artifact_sha256 must be a 64-character SHA-256 hex digest"
            )
        if not re.fullmatch(r"[0-9a-fA-F]{64}", config_hash):
            raise ValueError("config_hash must be a 64-character SHA-256 hex digest")
        metadata_json = json.dumps(
            dict(metadata or {}),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        timestamp = self._timestamp(created_at)
        values = (
            model_id,
            model_version,
            artifact_uri,
            artifact_sha256.lower(),
            config_hash.lower(),
            feature_schema_version,
            "CHALLENGER",
            timestamp,
            metadata_json,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT model_id, model_version, artifact_uri, artifact_sha256,
                       config_hash, feature_schema_version, status, created_at,
                       metadata_json
                FROM statistical_model_artifacts
                WHERE model_id=? AND model_version=?
                """,
                (model_id, model_version),
            ).fetchone()
            if row is not None:
                existing = tuple(row)
                expected = values
                # Registration does not downgrade a previously promoted artifact.
                if existing[:6] != expected[:6] or existing[8] != expected[8]:
                    raise ValueError("model version is already registered differently")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """
                INSERT INTO statistical_model_artifacts (
                    model_id, model_version, artifact_uri, artifact_sha256,
                    config_hash, feature_schema_version, status, created_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_champion(self, model_id: str) -> ModelArtifact | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM statistical_model_artifacts
                WHERE model_id=? AND status='CHAMPION'
                """,
                (model_id,),
            ).fetchone()
            return None if row is None else self._artifact(row)
        finally:
            connection.close()

    def list_artifacts(self, model_id: str) -> tuple[ModelArtifact, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM statistical_model_artifacts
                WHERE model_id=?
                ORDER BY created_at DESC, model_version DESC
                """,
                (model_id,),
            ).fetchall()
            return tuple(self._artifact(row) for row in rows)
        finally:
            connection.close()

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        model_id: str,
        model_version: str,
        action: str,
        decision: PromotionDecision,
        evidence: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO statistical_model_promotion_events (
                model_id, model_version, action, decision_json,
                evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                model_version,
                action,
                json.dumps(asdict(decision), sort_keys=True, allow_nan=False),
                json.dumps(dict(evidence), sort_keys=True, allow_nan=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def promotion_history(self, model_id: str) -> tuple[Mapping[str, Any], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT event_id, model_id, model_version, action,
                       decision_json, evidence_json, created_at
                FROM statistical_model_promotion_events
                WHERE model_id=?
                ORDER BY event_id ASC
                """,
                (model_id,),
            ).fetchall()
            return tuple(
                {
                    "event_id": row["event_id"],
                    "model_id": row["model_id"],
                    "model_version": row["model_version"],
                    "action": row["action"],
                    "decision": json.loads(row["decision_json"]),
                    "evidence": json.loads(row["evidence_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            )
        finally:
            connection.close()

    def promote(
        self,
        evidence: PromotionEvidence,
        *,
        policy: PromotionPolicy = PromotionPolicy(),
    ) -> PromotionDecision:
        """Promote an artifact and durably record the evidence/decision."""
        decision = evaluate_promotion(evidence, policy)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT status FROM statistical_model_artifacts
                WHERE model_id=? AND model_version=?
                """,
                (evidence.model_id, evidence.model_version),
            ).fetchone()
            if candidate is None:
                raise KeyError(
                    f"unregistered model artifact: "
                    f"{evidence.model_id}@{evidence.model_version}"
                )
            if not decision.eligible:
                self._insert_event(
                    connection,
                    model_id=evidence.model_id,
                    model_version=evidence.model_version,
                    action="PROMOTION_BLOCKED",
                    decision=decision,
                    evidence=asdict(evidence),
                )
                connection.execute("COMMIT")
                return decision
            if candidate["status"] == "CHAMPION":
                self._insert_event(
                    connection,
                    model_id=evidence.model_id,
                    model_version=evidence.model_version,
                    action="PROMOTION_NOOP",
                    decision=decision,
                    evidence=asdict(evidence),
                )
                connection.execute("COMMIT")
                return decision
            previous = connection.execute(
                """
                SELECT model_version FROM statistical_model_artifacts
                WHERE model_id=? AND status='CHAMPION'
                """,
                (evidence.model_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE statistical_model_artifacts
                SET status='ARCHIVED'
                WHERE model_id=? AND status='CHAMPION'
                """,
                (evidence.model_id,),
            )
            connection.execute(
                """
                UPDATE statistical_model_artifacts
                SET status='CHAMPION'
                WHERE model_id=? AND model_version=?
                """,
                (evidence.model_id, evidence.model_version),
            )
            event_evidence = asdict(evidence)
            event_evidence["previous_champion"] = (
                None if previous is None else previous["model_version"]
            )
            self._insert_event(
                connection,
                model_id=evidence.model_id,
                model_version=evidence.model_version,
                action="PROMOTED",
                decision=decision,
                evidence=event_evidence,
            )
            connection.execute("COMMIT")
            return decision
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def rollback(
        self,
        model_id: str,
        target_version: str,
        *,
        operator_approved: bool = False,
    ) -> ModelArtifact:
        """Restore a previous artifact only after explicit operator approval."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                """
                SELECT * FROM statistical_model_artifacts
                WHERE model_id=? AND model_version=?
                """,
                (model_id, target_version),
            ).fetchone()
            if target is None:
                raise KeyError(f"unknown model artifact: {model_id}@{target_version}")
            if not operator_approved:
                decision = PromotionDecision(
                    eligible=False,
                    reasons=("explicit_operator_approval_required",),
                )
                self._insert_event(
                    connection,
                    model_id=model_id,
                    model_version=target_version,
                    action="ROLLBACK_BLOCKED",
                    decision=decision,
                    evidence={"operator_approved": False},
                )
                connection.execute("COMMIT")
                raise PermissionError(
                    "explicit operator approval is required for rollback"
                )
            previous = connection.execute(
                """
                SELECT model_version FROM statistical_model_artifacts
                WHERE model_id=? AND status='CHAMPION'
                """,
                (model_id,),
            ).fetchone()
            if target["status"] != "CHAMPION":
                connection.execute(
                    """
                    UPDATE statistical_model_artifacts
                    SET status='ARCHIVED'
                    WHERE model_id=? AND status='CHAMPION'
                    """,
                    (model_id,),
                )
                connection.execute(
                    """
                    UPDATE statistical_model_artifacts
                    SET status='CHAMPION'
                    WHERE model_id=? AND model_version=?
                    """,
                    (model_id, target_version),
                )
            decision = PromotionDecision(eligible=True, reasons=())
            self._insert_event(
                connection,
                model_id=model_id,
                model_version=target_version,
                action=(
                    "ROLLBACK" if target["status"] != "CHAMPION" else "ROLLBACK_NOOP"
                ),
                decision=decision,
                evidence={
                    "operator_approved": True,
                    "previous_champion": (
                        None if previous is None else previous["model_version"]
                    ),
                },
            )
            promoted = connection.execute(
                """
                SELECT * FROM statistical_model_artifacts
                WHERE model_id=? AND model_version=?
                """,
                (model_id, target_version),
            ).fetchone()
            connection.execute("COMMIT")
            return self._artifact(promoted)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
