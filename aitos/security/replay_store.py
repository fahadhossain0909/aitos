"""Durable, fail-closed replay and idempotency ledger for signer intents.

Only hashes and bounded outcome metadata are persisted. Raw nonce values, exchange
credentials, signatures, request headers and complete exchange responses are not.
A row left in DISPATCHING after a crash is intentionally ambiguous and must never
be automatically resubmitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aitos.security.signing_policy import ValidatedOrderIntent


IntentStatus = Literal["reserved", "dispatching", "accepted", "rejected", "unknown"]
_TERMINAL = frozenset({"accepted", "rejected", "unknown"})
_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ReplayConflict(RuntimeError):
    """An intent, nonce, or client order ID was reused with different content."""


class ReplayStoreError(RuntimeError):
    """The durable replay ledger is unavailable or has an invalid transition."""


@dataclass(frozen=True)
class IntentReservation:
    intent_id: str
    account_alias: str
    client_order_id: str
    symbol: str
    fingerprint: str
    status: IntentStatus
    outcome_code: str | None
    created_at_ms: int
    created: bool


def _fingerprint(intent: ValidatedOrderIntent) -> str:
    canonical = {
        "schema_version": intent.schema_version,
        "intent_id": intent.intent_id,
        "account_alias": intent.account_alias,
        "symbol": intent.symbol,
        "side": intent.side,
        "order_type": intent.order_type,
        "quantity": format(intent.quantity, "f"),
        "limit_price": (
            format(intent.limit_price, "f") if intent.limit_price is not None else None
        ),
        "reduce_only": intent.reduce_only,
        "client_order_id": intent.client_order_id,
        "strategy_id": intent.strategy_id,
        "candidate_id": intent.candidate_id,
        "decision_id": intent.decision_id,
        "reason_code": intent.reason_code,
        "issued_at_ms": intent.issued_at_ms,
        "expires_at_ms": intent.expires_at_ms,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


class SQLiteReplayStore:
    """Single-host durable replay store using SQLite FULL synchronous commits.

    Use one database on a private local filesystem, not a shared network drive.
    A signer deployment must additionally protect the directory with OS ACLs,
    backups and exclusive process ownership. This store is not a distributed lock.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 5000,
        max_records: int = 2_000_000,
    ):
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 1 <= busy_timeout_ms <= 60_000
        ):
            raise ValueError("invalid_busy_timeout")
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or not 1 <= max_records <= 10_000_000
        ):
            raise ValueError("invalid_replay_capacity")
        raw_path = os.fspath(database_path)
        if raw_path == ":memory:":
            raise ValueError("durable_database_path_required")
        self.path = Path(raw_path).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("replay_directory_must_be_private")
        if self.path.is_symlink():
            raise ValueError("replay_database_must_not_be_symlink")
        self.max_records = max_records
        self._lock = threading.RLock()
        try:
            if os.name == "posix":
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError("replay_database_must_be_regular_file")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            self._connection = sqlite3.connect(
                str(self.path),
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            if os.name == "posix":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, sqlite3.Error) as exc:
            raise ReplayStoreError("replay_store_initialization_failed") from exc

    def _initialize(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signer_intent_ledger (
                intent_id TEXT PRIMARY KEY,
                account_alias TEXT NOT NULL,
                nonce_sha256 TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                fingerprint_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('reserved','dispatching','accepted','rejected','unknown')
                ),
                outcome_code TEXT,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                UNIQUE(account_alias, nonce_sha256),
                UNIQUE(account_alias, client_order_id)
            )
            """
        )
        columns = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(signer_intent_ledger)"
            ).fetchall()
        }
        if "symbol" not in columns:
            self._connection.execute(
                "ALTER TABLE signer_intent_ledger ADD COLUMN "
                "symbol TEXT NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS signer_intent_status_idx
            ON signer_intent_ledger(status, created_at_ms)
            """
        )

    def reserve(
        self, intent: ValidatedOrderIntent, *, now_ms: int
    ) -> IntentReservation:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("invalid_reservation_clock")
        fingerprint = _fingerprint(intent)
        nonce_hash = _nonce_digest(intent.nonce)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT * FROM signer_intent_ledger
                    WHERE intent_id = ?
                       OR (account_alias = ? AND nonce_sha256 = ?)
                       OR (account_alias = ? AND client_order_id = ?)
                    """,
                    (
                        intent.intent_id,
                        intent.account_alias,
                        nonce_hash,
                        intent.account_alias,
                        intent.client_order_id,
                    ),
                ).fetchall()
                if existing:
                    if (
                        len(existing) != 1
                        or existing[0]["intent_id"] != intent.intent_id
                        or existing[0]["account_alias"] != intent.account_alias
                        or existing[0]["nonce_sha256"] != nonce_hash
                        or existing[0]["client_order_id"] != intent.client_order_id
                        or existing[0]["fingerprint_sha256"] != fingerprint
                    ):
                        raise ReplayConflict(
                            "intent_identity_reused_with_different_content"
                        )
                    row = existing[0]
                    self._connection.execute("COMMIT")
                    return self._reservation(row, created=False)

                row_count = self._connection.execute(
                    "SELECT COUNT(*) FROM signer_intent_ledger"
                ).fetchone()[0]
                if row_count >= self.max_records:
                    raise ReplayStoreError("replay_ledger_capacity_reached")
                self._connection.execute(
                    """
                    INSERT INTO signer_intent_ledger (
                        intent_id, account_alias, nonce_sha256, client_order_id,
                        fingerprint_sha256, status, outcome_code,
                        created_at_ms, updated_at_ms, symbol
                    ) VALUES (?, ?, ?, ?, ?, 'reserved', NULL, ?, ?, ?)
                    """,
                    (
                        intent.intent_id,
                        intent.account_alias,
                        nonce_hash,
                        intent.client_order_id,
                        fingerprint,
                        now_ms,
                        now_ms,
                        intent.symbol,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM signer_intent_ledger WHERE intent_id = ?",
                    (intent.intent_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
                return self._reservation(row, created=True)
            except (ReplayConflict, ReplayStoreError):
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ReplayStoreError("replay_reservation_failed") from exc

    def mark_dispatching(self, intent_id: str, *, now_ms: int) -> None:
        self._transition(
            intent_id,
            expected="reserved",
            target="dispatching",
            now_ms=now_ms,
            outcome_code=None,
        )

    def finish(
        self,
        intent_id: str,
        *,
        status: Literal["accepted", "rejected", "unknown"],
        outcome_code: str,
        now_ms: int,
    ) -> None:
        if status not in _TERMINAL:
            raise ValueError("invalid_terminal_status")
        if not isinstance(outcome_code, str) or not _SAFE_CODE.fullmatch(outcome_code):
            raise ValueError("invalid_outcome_code")
        self._transition(
            intent_id,
            expected="dispatching",
            target=status,
            now_ms=now_ms,
            outcome_code=outcome_code,
        )

    def list_unresolved(self, *, limit: int = 1000) -> list[IntentReservation]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("invalid_unresolved_query_limit")
        with self._lock:
            try:
                rows = self._connection.execute(
                    """SELECT * FROM signer_intent_ledger
                    WHERE status IN ('dispatching','unknown')
                    ORDER BY created_at_ms ASC LIMIT ?""",
                    (limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise ReplayStoreError("replay_unresolved_query_failed") from exc
        return [self._reservation(row, created=False) for row in rows]

    def resolve_reconciled(
        self,
        intent_id: str,
        *,
        status: Literal["accepted", "rejected"],
        outcome_code: str,
        now_ms: int,
    ) -> None:
        if status not in {"accepted", "rejected"}:
            raise ValueError("invalid_reconciliation_status")
        if not isinstance(outcome_code, str) or not _SAFE_CODE.fullmatch(outcome_code):
            raise ValueError("invalid_outcome_code")
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid_reconciliation_clock")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """UPDATE signer_intent_ledger
                    SET status=?, outcome_code=?, updated_at_ms=?
                    WHERE intent_id=? AND status IN ('dispatching','unknown')""",
                    (status, outcome_code, now_ms, intent_id),
                )
                if cursor.rowcount != 1:
                    raise ReplayStoreError("intent_not_unresolved_or_missing")
                self._connection.execute("COMMIT")
            except ReplayStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ReplayStoreError("replay_reconciliation_failed") from exc

    def get(self, intent_id: str) -> IntentReservation | None:
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT * FROM signer_intent_ledger WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ReplayStoreError("replay_lookup_failed") from exc
        return self._reservation(row, created=False) if row else None

    def _transition(
        self,
        intent_id: str,
        *,
        expected: str,
        target: str,
        now_ms: int,
        outcome_code: str | None,
    ) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("invalid_transition_clock")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE signer_intent_ledger
                    SET status = ?, outcome_code = ?, updated_at_ms = ?
                    WHERE intent_id = ? AND status = ?
                    """,
                    (target, outcome_code, now_ms, intent_id, expected),
                )
                if cursor.rowcount != 1:
                    raise ReplayStoreError("invalid_or_missing_intent_transition")
                self._connection.execute("COMMIT")
            except ReplayStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ReplayStoreError("replay_transition_failed") from exc

    def _rollback(self) -> None:
        try:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _reservation(row: sqlite3.Row, *, created: bool) -> IntentReservation:
        return IntentReservation(
            intent_id=row["intent_id"],
            account_alias=row["account_alias"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            fingerprint=row["fingerprint_sha256"],
            status=row["status"],
            outcome_code=row["outcome_code"],
            created_at_ms=row["created_at_ms"],
            created=created,
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteReplayStore":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()
