"""Durable signer-side order-rate and realized-loss aggregation."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from aitos.security.risk_limits import AccountRiskSnapshot
from aitos.security.signing_policy import IntentRejected


class _EvidenceProvider(Protocol):
    def get_snapshot(
        self, account_alias: str, *, now_ms: int
    ) -> AccountRiskSnapshot: ...


class SQLiteSignerRiskState:
    """Conservatively overlay durable signer observations on exchange evidence.

    Every attempted authenticated intent consumes a durable order-window slot,
    including later-rejected intents. Realized net PnL events are idempotent by
    trade ID. Exchange evidence remains authoritative for positions/open orders;
    this store never invents exchange state.
    """

    def __init__(
        self,
        *,
        upstream: _EvidenceProvider,
        database_path: str | os.PathLike[str],
        max_orders_per_window: int,
        window_ms: int = 60_000,
    ) -> None:
        if not callable(getattr(upstream, "get_snapshot", None)):
            raise TypeError("trusted_account_evidence_provider_required")
        if (
            type(max_orders_per_window) is not int
            or not 1 <= max_orders_per_window <= 100_000
        ):
            raise ValueError("invalid_signer_order_rate_limit")
        if type(window_ms) is not int or not 100 <= window_ms <= 86_400_000:
            raise ValueError("invalid_signer_risk_window")
        raw = os.fspath(database_path)
        if raw == ":memory:":
            raise ValueError("durable_signer_risk_state_required")
        self.upstream = upstream
        self.max_orders_per_window = max_orders_per_window
        self.window_ms = window_ms
        self.path = Path(raw).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("signer_risk_db_must_not_be_symlink")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("signer_risk_directory_must_be_private")
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
                        raise ValueError("signer_risk_db_must_be_regular_file")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            self._db = sqlite3.connect(
                str(self.path), timeout=5, isolation_level=None, check_same_thread=False
            )
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS signer_order_windows (
                    account_alias TEXT NOT NULL,
                    window_start_ms INTEGER NOT NULL,
                    order_count INTEGER NOT NULL,
                    PRIMARY KEY(account_alias, window_start_ms)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS signer_realized_pnl (
                    account_alias TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    occurred_at_ms INTEGER NOT NULL,
                    net_amount TEXT NOT NULL,
                    PRIMARY KEY(account_alias, trade_id)
                )"""
            )
            if os.name == "posix":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("signer_risk_state_initialization_failed") from exc

    def get_snapshot(self, account_alias: str, *, now_ms: int) -> AccountRiskSnapshot:
        return self._get_snapshot(account_alias, now_ms=now_ms, count_order=True)

    def get_snapshot_for_intent(self, intent, *, now_ms: int) -> AccountRiskSnapshot:
        if (
            not isinstance(getattr(intent, "account_alias", None), str)
            or type(getattr(intent, "reduce_only", None)) is not bool
        ):
            raise ValueError("invalid_signer_risk_intent")
        return self._get_snapshot(
            intent.account_alias,
            now_ms=now_ms,
            count_order=not intent.reduce_only,
        )

    def _get_snapshot(
        self, account_alias: str, *, now_ms: int, count_order: bool
    ) -> AccountRiskSnapshot:
        if (
            type(account_alias) is not str
            or not account_alias
            or type(now_ms) is not int
            or now_ms < 0
        ):
            raise ValueError("invalid_signer_risk_snapshot_request")
        # Fetch exchange state before reserving the durable local order slot.
        snapshot = self.upstream.get_snapshot(account_alias, now_ms=now_ms)
        if (
            not isinstance(snapshot, AccountRiskSnapshot)
            or snapshot.account_alias != account_alias
        ):
            raise IntentRejected("account_evidence_mismatch")
        window_start = now_ms - (now_ms % self.window_ms)
        midnight = int(
            datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
            * 1000
        )
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    """SELECT order_count FROM signer_order_windows
                    WHERE account_alias=? AND window_start_ms=?""",
                    (account_alias, window_start),
                ).fetchone()
                count = row[0] if row else 0
                if count_order and count >= self.max_orders_per_window:
                    self._db.execute("ROLLBACK")
                    raise IntentRejected("order_rate_limit_reached")
                if count_order:
                    self._db.execute(
                        """INSERT INTO signer_order_windows VALUES (?,?,1)
                        ON CONFLICT(account_alias,window_start_ms)
                        DO UPDATE SET order_count=order_count+1""",
                        (account_alias, window_start),
                    )
                pnl_rows = self._db.execute(
                    """SELECT net_amount FROM signer_realized_pnl
                    WHERE account_alias=? AND occurred_at_ms>=? AND occurred_at_ms<=?""",
                    (account_alias, midnight, now_ms),
                ).fetchall()
                local_net = sum((Decimal(row[0]) for row in pnl_rows), Decimal("0"))
                self._db.execute(
                    "DELETE FROM signer_order_windows WHERE window_start_ms < ?",
                    (window_start - self.window_ms,),
                )
                self._db.execute("COMMIT")
            except IntentRejected:
                raise
            except (sqlite3.Error, InvalidOperation, ValueError) as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise RuntimeError("signer_risk_state_unavailable") from exc

        local_daily_loss = max(Decimal("0"), -local_net)
        return replace(
            snapshot,
            daily_loss=snapshot.daily_loss + local_daily_loss,
            order_count_window=snapshot.order_count_window + count,
        )

    def record_realized_pnl(
        self,
        account_alias: str,
        trade_id: str,
        net_amount: Decimal,
        *,
        occurred_at_ms: int,
    ) -> bool:
        """Record signed net PnL (including fees/funding) exactly once per trade."""
        if (
            type(account_alias) is not str
            or not account_alias
            or type(trade_id) is not str
            or not trade_id
            or len(trade_id) > 128
            or not isinstance(net_amount, Decimal)
            or not net_amount.is_finite()
            or type(occurred_at_ms) is not int
            or occurred_at_ms < 0
        ):
            raise ValueError("invalid_signer_realized_pnl_event")
        with self._lock:
            try:
                cursor = self._db.execute(
                    """INSERT OR IGNORE INTO signer_realized_pnl
                    VALUES (?,?,?,?)""",
                    (
                        account_alias,
                        trade_id,
                        occurred_at_ms,
                        format(net_amount, "f"),
                    ),
                )
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise RuntimeError("signer_realized_pnl_persist_failed") from exc

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
