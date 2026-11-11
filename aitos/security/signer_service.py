"""Persistent, mTLS-authenticated paper signer RPC service.

The listener accepts one bounded JSON request per TLS connection. Client identity
comes only from the TLS peer certificate. Enrollment is an explicit local
administrative operation; this module does not issue X.509 certificates or expose
an unauthenticated enrollment endpoint. The injected processor must be paper-only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import stat
import ssl
import threading
from pathlib import Path
from typing import Callable

from aitos.security.paper_processor import PaperOnlyIntentProcessor
from aitos.security.signer_rpc_protocol import (
    ClientAuthorizationRegistry,
    ClientGrant,
    SignerProtocolRejected,
    authenticate_signer_request,
)

_IDENTITY = re.compile(r"spiffe://[A-Za-z0-9._/-]{1,200}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{2,30}\Z")


class SignerServiceError(RuntimeError):
    """Persistent signer service state is unavailable or invalid."""


class SQLiteSignerRegistry:
    """Durable local enrollment, rotation and revocation registry.

    Certificate issuance and private-key delivery remain outside this service:
    an administrator enrolls the CA-issued certificate serial and URI SAN here.
    Multiple serials may overlap during rotation; revocation always wins.
    """

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        raw = os.fspath(database_path)
        if raw == ":memory:":
            raise ValueError("durable_signer_registry_required")
        self.path = Path(raw).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("signer_registry_must_not_be_symlink")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("signer_registry_directory_must_be_private")
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
                        raise ValueError("signer_registry_must_be_regular_file")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            self._db = sqlite3.connect(
                str(self.path), timeout=5, isolation_level=None, check_same_thread=False
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS signer_client_grants (
                    identity TEXT PRIMARY KEY,
                    account_alias TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    serials_json TEXT NOT NULL,
                    revoked_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    valid_from_ms INTEGER NOT NULL,
                    valid_until_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    serial_validity_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            columns = {
                row["name"]
                for row in self._db.execute(
                    "PRAGMA table_info(signer_client_grants)"
                ).fetchall()
            }
            if "serial_validity_json" not in columns:
                self._db.execute(
                    "ALTER TABLE signer_client_grants ADD COLUMN "
                    "serial_validity_json TEXT NOT NULL DEFAULT '{}'"
                )
            if os.name == "posix":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, sqlite3.Error) as exc:
            raise SignerServiceError("signer_registry_initialization_failed") from exc

    @staticmethod
    def _validate(identity: str, account_alias: str, symbols, serials) -> None:
        if type(identity) is not str or not _IDENTITY.fullmatch(identity):
            raise ValueError("invalid_signer_identity")
        if type(account_alias) is not str or not _ALIAS.fullmatch(account_alias):
            raise ValueError("invalid_signer_account_alias")
        if (
            type(symbols) not in (set, frozenset, tuple, list)
            or not symbols
            or any(type(s) is not str or not _SYMBOL.fullmatch(s) for s in symbols)
        ):
            raise ValueError("invalid_signer_symbol_grant")
        if (
            type(serials) not in (set, frozenset, tuple, list)
            or not serials
            or any(
                type(s) is not str or not re.fullmatch(r"[0-9A-Fa-f]{1,64}", s)
                for s in serials
            )
        ):
            raise ValueError("invalid_signer_certificate_serial")

    def enroll(
        self,
        *,
        identity: str,
        account_alias: str,
        allowed_symbols,
        certificate_serial: str,
        valid_from_ms: int,
        valid_until_ms: int,
        now_ms: int,
    ) -> ClientGrant:
        """Enroll an already CA-issued certificate; never accepts key material."""
        self._validate(identity, account_alias, allowed_symbols, [certificate_serial])
        if (
            type(valid_from_ms) is not int
            or type(valid_until_ms) is not int
            or type(now_ms) is not int
            or valid_from_ms < 0
            or valid_until_ms <= valid_from_ms
            or now_ms < 0
        ):
            raise ValueError("invalid_signer_certificate_validity")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM signer_client_grants WHERE identity=?", (identity,)
                ).fetchone()
                if row:
                    if row["account_alias"] != account_alias:
                        raise ValueError("signer_identity_account_binding_immutable")
                    symbols = set(json.loads(row["symbols_json"]))
                    if symbols != set(allowed_symbols):
                        raise ValueError("use_replace_grant_for_scope_change")
                    serials = set(json.loads(row["serials_json"]))
                    revoked = set(json.loads(row["revoked_json"]))
                    serial = certificate_serial.upper()
                    if serial in revoked:
                        raise ValueError(
                            "revoked_certificate_serial_cannot_be_reenrolled"
                        )
                    if serial in serials:
                        raise ValueError("certificate_serial_already_enrolled")
                    validity = json.loads(row["serial_validity_json"] or "{}")
                    if not validity:
                        validity = {
                            old_serial: [
                                row["valid_from_ms"],
                                row["valid_until_ms"],
                            ]
                            for old_serial in serials
                        }
                    serials.add(serial)
                    validity[serial] = [valid_from_ms, valid_until_ms]
                    self._write(
                        identity,
                        account_alias,
                        symbols,
                        serials,
                        revoked,
                        validity,
                        bool(row["enabled"]),
                        min(valid_from_ms, row["valid_from_ms"]),
                        max(valid_until_ms, row["valid_until_ms"]),
                        now_ms,
                    )
                else:
                    self._write(
                        identity,
                        account_alias,
                        set(allowed_symbols),
                        {certificate_serial.upper()},
                        set(),
                        {certificate_serial.upper(): [valid_from_ms, valid_until_ms]},
                        True,
                        valid_from_ms,
                        valid_until_ms,
                        now_ms,
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
        return self.get_registry()._grants[identity]

    def revoke(self, identity: str, certificate_serial: str, *, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid_signer_registry_clock")
        if type(certificate_serial) is not str or not re.fullmatch(
            r"[0-9A-Fa-f]{1,64}", certificate_serial
        ):
            raise ValueError("invalid_signer_certificate_serial")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM signer_client_grants WHERE identity=?", (identity,)
                ).fetchone()
                if row is None:
                    raise ValueError("signer_identity_not_enrolled")
                serials = set(json.loads(row["serials_json"]))
                if certificate_serial.upper() not in serials:
                    raise ValueError("signer_certificate_serial_not_enrolled")
                revoked = set(json.loads(row["revoked_json"]))
                revoked.add(certificate_serial.upper())
                self._write(
                    identity,
                    row["account_alias"],
                    set(json.loads(row["symbols_json"])),
                    set(json.loads(row["serials_json"])),
                    revoked,
                    json.loads(row["serial_validity_json"] or "{}"),
                    bool(row["enabled"]),
                    row["valid_from_ms"],
                    row["valid_until_ms"],
                    now_ms,
                )
                self._db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def disable(self, identity: str, *, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid_signer_registry_clock")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM signer_client_grants WHERE identity=?", (identity,)
                ).fetchone()
                if row is None:
                    raise ValueError("signer_identity_not_enrolled")
                self._write(
                    identity,
                    row["account_alias"],
                    set(json.loads(row["symbols_json"])),
                    set(json.loads(row["serials_json"])),
                    set(json.loads(row["revoked_json"])),
                    json.loads(row["serial_validity_json"] or "{}"),
                    False,
                    row["valid_from_ms"],
                    row["valid_until_ms"],
                    now_ms,
                )
                self._db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def enable(self, identity: str, *, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid_signer_registry_clock")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM signer_client_grants WHERE identity=?", (identity,)
                ).fetchone()
                if row is None:
                    raise ValueError("signer_identity_not_enrolled")
                self._write(
                    identity,
                    row["account_alias"],
                    set(json.loads(row["symbols_json"])),
                    set(json.loads(row["serials_json"])),
                    set(json.loads(row["revoked_json"])),
                    json.loads(row["serial_validity_json"] or "{}"),
                    True,
                    row["valid_from_ms"],
                    row["valid_until_ms"],
                    now_ms,
                )
                self._db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def _write(
        self,
        identity,
        account,
        symbols,
        serials,
        revoked,
        serial_validity,
        enabled,
        valid_from,
        valid_until,
        now_ms,
    ):
        self._db.execute(
            """INSERT INTO signer_client_grants (
                identity, account_alias, symbols_json, serials_json, revoked_json,
                enabled, valid_from_ms, valid_until_ms, updated_at_ms,
                serial_validity_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
              account_alias=excluded.account_alias,
              symbols_json=excluded.symbols_json,
              serials_json=excluded.serials_json,
              revoked_json=excluded.revoked_json,
              enabled=excluded.enabled,
              valid_from_ms=excluded.valid_from_ms,
              valid_until_ms=excluded.valid_until_ms,
              updated_at_ms=excluded.updated_at_ms,
              serial_validity_json=excluded.serial_validity_json""",
            (
                identity,
                account,
                json.dumps(sorted(symbols)),
                json.dumps(sorted(serials)),
                json.dumps(sorted(revoked)),
                int(enabled),
                valid_from,
                valid_until,
                now_ms,
                json.dumps(serial_validity, sort_keys=True),
            ),
        )

    def get_registry(self) -> ClientAuthorizationRegistry:
        with self._lock:
            try:
                rows = self._db.execute("SELECT * FROM signer_client_grants").fetchall()
                grants = {
                    row["identity"]: ClientGrant(
                        identity=row["identity"],
                        account_alias=row["account_alias"],
                        allowed_symbols=frozenset(json.loads(row["symbols_json"])),
                        allowed_serials=frozenset(json.loads(row["serials_json"])),
                        revoked_serials=frozenset(json.loads(row["revoked_json"])),
                        enabled=bool(row["enabled"]),
                        valid_from_ms=row["valid_from_ms"],
                        valid_until_ms=row["valid_until_ms"],
                        serial_validity={
                            serial: tuple(window)
                            for serial, window in (
                                json.loads(row["serial_validity_json"] or "{}")
                                or {
                                    serial: [
                                        row["valid_from_ms"],
                                        row["valid_until_ms"],
                                    ]
                                    for serial in json.loads(row["serials_json"])
                                }
                            ).items()
                        },
                    )
                    for row in rows
                }
                return ClientAuthorizationRegistry(grants) if grants else None
            except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                raise SignerServiceError("signer_registry_read_failed") from exc

    def _rollback(self) -> None:
        try:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class SQLiteSignerRateLimiter:
    """Atomic, restart-safe fixed-window limiter scoped by identity and account."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        limit: int,
        window_ms: int,
        reduce_only_limit: int | None = None,
    ):
        if type(limit) is not int or not 1 <= limit <= 100_000:
            raise ValueError("invalid_signer_rate_limit")
        if type(window_ms) is not int or not 100 <= window_ms <= 86_400_000:
            raise ValueError("invalid_signer_rate_window")
        if reduce_only_limit is None:
            reduce_only_limit = min(max(limit * 10, 100), 100_000)
        if type(reduce_only_limit) is not int or not 1 <= reduce_only_limit <= 100_000:
            raise ValueError("invalid_reduce_only_rate_limit")
        self.limit, self.window_ms = limit, window_ms
        self.reduce_only_limit = reduce_only_limit
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("signer_rate_db_must_not_be_symlink")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("signer_rate_directory_must_be_private")
        self._lock = threading.RLock()
        try:
            if os.name == "posix":
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(fd)
            self._db = sqlite3.connect(
                str(self.path), timeout=5, isolation_level=None, check_same_thread=False
            )
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS signer_rate_windows (
                    identity TEXT NOT NULL, account_alias TEXT NOT NULL,
                    window_start_ms INTEGER NOT NULL, request_count INTEGER NOT NULL,
                    PRIMARY KEY(identity, account_alias, window_start_ms)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS signer_reduce_only_rate_windows (
                    identity TEXT NOT NULL, account_alias TEXT NOT NULL,
                    window_start_ms INTEGER NOT NULL, request_count INTEGER NOT NULL,
                    PRIMARY KEY(identity, account_alias, window_start_ms)
                )"""
            )
            if os.name == "posix":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, sqlite3.Error) as exc:
            raise SignerServiceError(
                "signer_rate_limiter_initialization_failed"
            ) from exc

    def consume(
        self,
        identity: str,
        account_alias: str,
        *,
        now_ms: int,
        is_reduce_only: bool = False,
    ) -> None:
        if type(is_reduce_only) is not bool:
            raise ValueError("invalid_signer_rate_class")
        if (
            type(identity) is not str
            or not _IDENTITY.fullmatch(identity)
            or type(account_alias) is not str
            or not _ALIAS.fullmatch(account_alias)
            or type(now_ms) is not int
            or now_ms < 0
        ):
            raise ValueError("invalid_signer_rate_key")
        start = now_ms - (now_ms % self.window_ms)
        table = (
            "signer_reduce_only_rate_windows"
            if is_reduce_only
            else "signer_rate_windows"
        )
        limit = self.reduce_only_limit if is_reduce_only else self.limit
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    f"""SELECT request_count FROM {table}
                    WHERE identity=? AND account_alias=? AND window_start_ms=?""",
                    (identity, account_alias, start),
                ).fetchone()
                count = row[0] if row else 0
                if count >= limit:
                    self._db.execute("ROLLBACK")
                    raise SignerProtocolRejected("signer_rate_limit_exceeded")
                self._db.execute(
                    f"""INSERT INTO {table} VALUES (?,?,?,1)
                    ON CONFLICT(identity,account_alias,window_start_ms)
                    DO UPDATE SET request_count=request_count+1""",
                    (identity, account_alias, start),
                )
                # Retain the active window and one previous window for audit/debug.
                self._db.execute(
                    f"DELETE FROM {table} WHERE window_start_ms < ?",
                    (start - self.window_ms,),
                )
                self._db.execute("COMMIT")
            except SignerProtocolRejected:
                raise
            except sqlite3.Error as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise SignerServiceError("signer_rate_limiter_unavailable") from exc

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class PaperSignerRPCServer:
    """Real asyncio TLS socket listener; accepts only TLS 1.3 mTLS clients."""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext,
        registry: SQLiteSignerRegistry,
        rate_limiter: SQLiteSignerRateLimiter,
        processor: PaperOnlyIntentProcessor,
        max_body_bytes: int = 16_384,
        request_timeout_seconds: float = 5.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(ssl_context, ssl.SSLContext):
            raise TypeError("tls_server_context_required")
        if (
            ssl_context.protocol != ssl.PROTOCOL_TLS_SERVER
            or ssl_context.verify_mode != ssl.CERT_REQUIRED
            or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_3
        ):
            raise ValueError("tls13_mutual_authentication_required")
        if not isinstance(registry, SQLiteSignerRegistry):
            raise TypeError("persistent_signer_registry_required")
        if not isinstance(rate_limiter, SQLiteSignerRateLimiter):
            raise TypeError("persistent_signer_rate_limiter_required")
        if not isinstance(processor, PaperOnlyIntentProcessor):
            raise TypeError("paper_only_processor_required")
        if not getattr(processor.adapter, "paper_only", False):
            raise ValueError("non_paper_dispatch_forbidden")
        if processor.risk_limits is None or processor.account_evidence_provider is None:
            raise ValueError("independent_signer_risk_evidence_required")
        if type(max_body_bytes) is not int or not 256 <= max_body_bytes <= 65_536:
            raise ValueError("invalid_signer_rpc_body_limit")
        if not 0.1 <= request_timeout_seconds <= 30:
            raise ValueError("invalid_signer_rpc_timeout")
        self.ssl_context = ssl_context
        self.registry, self.rate_limiter, self.processor = (
            registry,
            rate_limiter,
            processor,
        )
        self.max_body_bytes = max_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.clock_ms = clock_ms or (lambda: __import__("time").time_ns() // 1_000_000)
        self._server = None
        self._connection_slots = asyncio.Semaphore(32)

    async def start(self, host: str, port: int) -> None:
        if self._server is not None:
            raise RuntimeError("signer_rpc_server_already_started")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid_signer_rpc_port")
        self._server = await asyncio.start_server(
            self._handle,
            host=host,
            port=port,
            ssl=self.ssl_context,
            limit=self.max_body_bytes + 1,
            backlog=64,
            ssl_handshake_timeout=5.0,
            start_serving=True,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("signer_rpc_server_not_started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        async with self._connection_slots:
            await self._handle_request(reader, writer)

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        response = {"status": "rejected", "outcome_code": "signer_request_rejected"}
        try:
            tls_object = writer.get_extra_info("ssl_object")
            if tls_object is None:
                raise SignerProtocolRejected("mutual_tls_required")
            peer = tls_object.getpeercert()
            if not peer:
                raise SignerProtocolRejected("client_certificate_required")
            raw = await asyncio.wait_for(
                reader.readuntil(b"\n"), timeout=self.request_timeout_seconds
            )
            if len(raw) - 1 > self.max_body_bytes:
                raise SignerProtocolRejected("signer_request_too_large")
            registry = self.registry.get_registry()
            if registry is None:
                raise SignerProtocolRejected("no_signer_clients_enrolled")
            now_ms = self.clock_ms()
            request = authenticate_signer_request(
                raw[:-1],
                peer_certificate=peer,
                registry=registry,
                now_ms=now_ms,
                max_body_bytes=self.max_body_bytes,
            )
            is_reduce_only = request.intent.get("reduce_only") is True
            self.rate_limiter.consume(
                request.authenticated_identity,
                request.account_alias,
                now_ms=now_ms,
                is_reduce_only=is_reduce_only,
            )
            intent_id = request.intent.get("intent_id")
            existing = (
                self.processor.replay_store.get(intent_id)
                if isinstance(intent_id, str)
                else None
            )
            if (
                existing is None
                and self.processor.replay_store.list_unresolved(limit=1)
            ):
                raise SignerProtocolRejected(
                    "unresolved_execution_requires_reconciliation"
                )
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.processor.process, dict(request.intent), now_ms=now_ms
                    ),
                    timeout=self.request_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # The worker thread may still finish after the client timeout.
                # The durable replay row prevents a retry from dispatching twice.
                response = {
                    "status": "unknown",
                    "outcome_code": "paper_dispatch_timeout_ambiguous",
                }
            else:
                response = {
                    "status": result.status,
                    "outcome_code": result.outcome_code or "none",
                    "duplicate": result.duplicate,
                }
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
        ):
            response = {
                "status": "rejected",
                "outcome_code": "request_incomplete_or_timeout",
            }
        except (SignerProtocolRejected, ValueError):
            response = {"status": "rejected", "outcome_code": "signer_policy_rejected"}
        except Exception:
            # Never return exception strings, account data, credentials or stack traces.
            response = {"status": "unknown", "outcome_code": "signer_internal_failure"}
        try:
            writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
