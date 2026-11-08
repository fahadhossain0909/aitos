"""Production adapters for fail-closed drift monitoring and operator governance.

No adapter is activated implicitly. Deployments must inject an authenticated
ClickHouse client, configure an alert endpoint, and keep the approval signing
key in the platform secret store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class FeatureBatchProvider(Protocol):
    def __call__(
        self,
    ) -> tuple[Mapping[str, Sequence[float]], Mapping[str, Sequence[float]]]: ...


class ClickHouseDriftFeatureBatchProvider:
    """Build disjoint reference/current batches from closed experience rows.

    Feature names are explicit allow-list entries. Empty, stale, malformed,
    non-finite or undersized batches raise; the scheduler therefore cannot
    silently interpret missing data as NORMAL drift.
    """

    def __init__(
        self,
        store,
        *,
        lookback: timedelta = timedelta(days=30),
        window: timedelta = timedelta(days=7),
        min_observations: int = 200,
        sources: Sequence[str] = ("paper", "live"),
        feature_names: Sequence[str],
        max_staleness: timedelta = timedelta(days=2),
        clock=None,
    ) -> None:
        if lookback <= window or window.total_seconds() <= 0:
            raise ValueError("lookback must exceed a positive window")
        if min_observations < 20:
            raise ValueError("min_observations must be >= 20")
        if not feature_names or any(not str(name).strip() for name in feature_names):
            raise ValueError("an explicit non-empty feature allow-list is required")
        if max_staleness <= timedelta(0) or max_staleness > window:
            raise ValueError("max_staleness must be positive and no larger than window")
        if not sources or any(s not in {"paper", "live", "backtest"} for s in sources):
            raise ValueError("sources must contain supported experience sources")
        self.store = store
        self.lookback = lookback
        self.window = window
        self.min_observations = int(min_observations)
        self.sources = tuple(sources)
        self.feature_names = tuple(dict.fromkeys(str(name) for name in feature_names))
        self.max_staleness = max_staleness
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("feature provider clock must be timezone-aware")
        end = now.astimezone(timezone.utc)
        current_start = end - self.window
        reference_start = current_start - self.lookback
        # Query non-overlapping windows separately. Ascending LIMIT on a
        # combined query can truncate the newest observations behind backlog.
        reference_rows = self.store.query_closed_experiences(
            reference_start,
            current_start,
            sources=self.sources,
            limit=1_000_000,
        )
        current_rows = self.store.query_closed_experiences(
            current_start,
            end,
            sources=self.sources,
            limit=1_000_000,
        )
        reference: dict[str, list[float]] = {}
        current: dict[str, list[float]] = {}
        latest_current = None
        for row in chain(reference_rows, current_rows):
            timestamp = row.get("timestamp")
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError("experience row has missing/naive timestamp")
            timestamp = timestamp.astimezone(timezone.utc)
            if timestamp < reference_start or timestamp >= end:
                continue
            raw = row.get("features", row.get("features_json"))
            if isinstance(raw, str):
                raw = json.loads(raw)
            if not isinstance(raw, Mapping):
                raise ValueError("experience features must be a JSON object")
            is_current = timestamp >= current_start
            target = current if is_current else reference
            if is_current and (latest_current is None or timestamp > latest_current):
                latest_current = timestamp
            for name in self.feature_names:
                if name not in raw:
                    raise RuntimeError(f"configured drift feature missing: {name}")
                value = raw[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RuntimeError(
                        f"configured drift feature is non-numeric: {name}"
                    )
                number = float(value)
                if not math.isfinite(number):
                    raise RuntimeError(
                        f"configured drift feature is non-finite: {name}"
                    )
                target.setdefault(str(name), []).append(number)
        if not reference or not current:
            raise RuntimeError("drift feature windows are empty; fail closed")
        if latest_current is None or end - latest_current > self.max_staleness:
            raise RuntimeError("current drift feature batch is stale; fail closed")
        common = sorted(set(reference).intersection(current))
        if not common:
            raise RuntimeError("no common numeric features across drift windows")
        for name in common:
            if (
                len(reference[name]) < self.min_observations
                or len(current[name]) < self.min_observations
            ):
                raise RuntimeError(f"insufficient drift samples for feature {name}")
        if self.feature_names and set(common) != set(self.feature_names):
            missing = sorted(set(self.feature_names) - set(common))
            raise RuntimeError(f"configured drift features missing: {missing}")
        return (
            {name: reference[name] for name in common},
            {name: current[name] for name in common},
        )


@dataclass(frozen=True)
class DriftAlert:
    status: str
    max_psi: float
    drifted_features: tuple[str, ...]
    reasons: tuple[str, ...]
    observed_at: str
    model_context: str = "statistical-governance"


class LoggingDriftAlertSink:
    """Safe default sink: structured logging only; never claims external delivery."""

    def __call__(self, action) -> None:
        import logging

        logging.getLogger("aitos.statistics.drift").warning(
            "statistical feature drift: %s",
            json.dumps(
                {
                    "status": action.status,
                    "max_psi": action.report.max_psi,
                    "drifted_features": list(action.report.drifted_features),
                    "reasons": list(action.reasons),
                },
                sort_keys=True,
            ),
        )


class WebhookDriftAlertSink:
    """POST a minimal JSON alert to an operator-owned HTTPS endpoint."""

    def __init__(
        self, endpoint: str, *, bearer_token: str, timeout_seconds: float = 5.0
    ):
        if not endpoint.startswith("https://"):
            raise ValueError("alert endpoint must use HTTPS")
        if not bearer_token.strip():
            raise ValueError("webhook bearer token is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self.endpoint = endpoint
        self._token = bearer_token
        self.timeout_seconds = timeout_seconds

    def __call__(self, action) -> None:
        body = json.dumps(
            {
                "status": action.status,
                "max_psi": action.report.max_psi,
                "drifted_features": list(action.report.drifted_features),
                "reasons": list(action.reasons),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
            allow_nan=False,
        ).encode()
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )

        class RejectRedirects(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        try:
            opener = build_opener(RejectRedirects)
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"alert sink returned HTTP {response.status}")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError("drift alert delivery failed") from exc


@dataclass(frozen=True)
class OperatorIdentity:
    """Identity claims created by trusted authentication middleware, not request JSON."""

    issuer: str
    subject: str
    roles: tuple[str, ...]
    authenticated: bool = True


class OperatorApprovalService:
    """Short-lived, candidate-bound, single-use approval tokens with SQLite audit.

    The caller must supply an OperatorIdentity verified by the deployment's
    OIDC/SAML/mTLS middleware. This class is not an authentication provider.
    """

    def __init__(
        self,
        registry,
        governance_runtime,
        *,
        database_path: str | Path,
        signing_secret: str,
        allowed_issuers: Sequence[str],
        approver_roles: Sequence[str] = ("aitos-model-approver",),
        token_ttl_seconds: int = 300,
    ) -> None:
        if len(signing_secret.encode()) < 32:
            raise ValueError("approval signing secret must be at least 32 bytes")
        if not allowed_issuers or not approver_roles:
            raise ValueError("allowed issuers and approver roles are required")
        if not 30 <= token_ttl_seconds <= 900:
            raise ValueError("approval token TTL must be between 30 and 900 seconds")
        self.registry = registry
        self.runtime = governance_runtime
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = signing_secret.encode()
        self.allowed_issuers = frozenset(allowed_issuers)
        self.approver_roles = frozenset(approver_roles)
        self.token_ttl_seconds = token_ttl_seconds
        db = self._connect()
        try:
            db.execute(
                """CREATE TABLE IF NOT EXISTS operator_approval_nonces (
                nonce_hash TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                model_id TEXT NOT NULL, model_version TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL, approval_reason TEXT NOT NULL,
                expires_at INTEGER NOT NULL, consumed_at INTEGER, decision_json TEXT
            )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS operator_approval_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                nonce_hash TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                model_id TEXT NOT NULL, model_version TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL, approval_reason TEXT NOT NULL,
                event TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL
            )"""
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS operator_approval_audit_no_update
                BEFORE UPDATE ON operator_approval_audit
                BEGIN SELECT RAISE(ABORT, 'approval audit is append-only'); END"""
            )
            db.execute(
                """CREATE TRIGGER IF NOT EXISTS operator_approval_audit_no_delete
                BEFORE DELETE ON operator_approval_audit
                BEGIN SELECT RAISE(ABORT, 'approval audit is append-only'); END"""
            )
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _validate_identity(self, identity: OperatorIdentity) -> None:
        if (
            not isinstance(identity, OperatorIdentity)
            or not identity.authenticated
            or identity.issuer not in self.allowed_issuers
            or not identity.subject.strip()
            or not self.approver_roles.intersection(identity.roles)
        ):
            raise PermissionError("authenticated model-approver role is required")

    def _candidate(self, model_id: str, model_version: str):
        matches = [
            a
            for a in self.registry.list_artifacts(model_id)
            if a.model_version == model_version
        ]
        if len(matches) != 1 or matches[0].status == "ARCHIVED":
            raise KeyError("candidate artifact is missing or archived")
        return matches[0]

    def issue_approval(
        self,
        identity: OperatorIdentity,
        *,
        model_id: str,
        model_version: str,
        reason: str,
        now: int | None = None,
    ) -> str:
        self._validate_identity(identity)
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500:
            raise ValueError("approval reason must contain 8 to 500 characters")
        artifact = self._candidate(model_id, model_version)
        issued = int(time.time() if now is None else now)
        nonce = secrets.token_urlsafe(24)
        payload = {
            "iss": identity.issuer,
            "sub": identity.subject,
            "model_id": model_id,
            "model_version": model_version,
            "artifact_sha256": artifact.artifact_sha256,
            "reason": reason.strip(),
            "iat": issued,
            "exp": issued + self.token_ttl_seconds,
            "nonce": nonce,
        }
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO operator_approval_nonces "
                "(nonce_hash,issuer,subject,model_id,model_version,artifact_sha256,"
                "approval_reason,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    nonce_hash,
                    identity.issuer,
                    identity.subject,
                    model_id,
                    model_version,
                    artifact.artifact_sha256,
                    payload["reason"],
                    payload["exp"],
                ),
            )
            db.execute(
                "INSERT INTO operator_approval_audit "
                "(nonce_hash,issuer,subject,model_id,model_version,artifact_sha256,"
                "approval_reason,event,created_at,details_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    nonce_hash,
                    identity.issuer,
                    identity.subject,
                    model_id,
                    model_version,
                    artifact.artifact_sha256,
                    payload["reason"],
                    "ISSUED",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps({"roles": list(identity.roles)}, sort_keys=True),
                ),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return f"{encoded}.{signature}"

    def approve_and_promote(self, identity: OperatorIdentity, token: str, evidence):
        self._validate_identity(identity)
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected = hmac.new(
                self._secret, encoded.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, supplied_signature):
                raise ValueError("invalid signature")
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
        except Exception as exc:
            raise PermissionError("invalid operator approval token") from exc
        now = int(time.time())
        if (
            payload.get("iss") != identity.issuer
            or payload.get("sub") != identity.subject
            or payload.get("exp", 0) <= now
            or payload.get("iat", now + 1) > now
        ):
            raise PermissionError(
                "approval token is expired or bound to another operator"
            )
        artifact = self._candidate(payload["model_id"], payload["model_version"])
        if artifact.artifact_sha256 != payload["artifact_sha256"]:
            raise PermissionError("candidate artifact changed after approval")
        if (
            evidence.model_id != payload["model_id"]
            or evidence.model_version != payload["model_version"]
        ):
            raise PermissionError("approval token does not match promotion evidence")
        nonce_hash = hashlib.sha256(payload["nonce"].encode()).hexdigest()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT issuer,subject,model_id,model_version,artifact_sha256,"
                "approval_reason,expires_at,consumed_at FROM operator_approval_nonces "
                "WHERE nonce_hash=?",
                (nonce_hash,),
            ).fetchone()
            if (
                row is None
                or row[0] != identity.issuer
                or row[1] != identity.subject
                or row[2] != payload["model_id"]
                or row[3] != payload["model_version"]
                or row[4] != artifact.artifact_sha256
                or row[5] != payload.get("reason")
                or row[6] < now
                or row[7] is not None
            ):
                raise PermissionError(
                    "approval nonce is invalid, expired or already consumed"
                )
            db.execute(
                "UPDATE operator_approval_nonces SET consumed_at=? WHERE nonce_hash=?",
                (now, nonce_hash),
            )
            db.execute(
                "INSERT INTO operator_approval_audit "
                "(nonce_hash,issuer,subject,model_id,model_version,artifact_sha256,"
                "approval_reason,event,created_at,details_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    nonce_hash,
                    identity.issuer,
                    identity.subject,
                    payload["model_id"],
                    payload["model_version"],
                    artifact.artifact_sha256,
                    payload["reason"],
                    "CONSUMED",
                    datetime.now(timezone.utc).isoformat(),
                    "{}",
                ),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        approved_evidence = replace(evidence, operator_approved=True)
        decision = self.runtime.promote_challenger(
            approved_evidence, operator_approved=True
        )
        audit_db = self._connect()
        try:
            audit_db.execute(
                "INSERT INTO operator_approval_audit "
                "(nonce_hash,issuer,subject,model_id,model_version,artifact_sha256,"
                "approval_reason,event,created_at,details_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    nonce_hash,
                    identity.issuer,
                    identity.subject,
                    payload["model_id"],
                    payload["model_version"],
                    artifact.artifact_sha256,
                    payload["reason"],
                    "PROMOTION_DECISION",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(
                        {
                            "eligible": decision.eligible,
                            "reasons": list(decision.reasons),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        finally:
            audit_db.close()
        return decision

    def audit_history(self, model_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return append-only approval events for one model."""
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT audit_id,nonce_hash,issuer,subject,model_id,model_version,"
                "artifact_sha256,approval_reason,event,created_at,details_json "
                "FROM operator_approval_audit WHERE model_id=? ORDER BY audit_id",
                (model_id,),
            ).fetchall()
            return tuple(
                {
                    "audit_id": row[0],
                    "nonce_hash": row[1],
                    "issuer": row[2],
                    "subject": row[3],
                    "model_id": row[4],
                    "model_version": row[5],
                    "artifact_sha256": row[6],
                    "approval_reason": row[7],
                    "event": row[8],
                    "created_at": row[9],
                    "details": json.loads(row[10]),
                }
                for row in rows
            )
        finally:
            db.close()
