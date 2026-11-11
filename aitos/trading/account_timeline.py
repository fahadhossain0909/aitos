"""Opt-in durable observation timeline. Not an execution/recovery authority.

One explicitly selected account per directory. Immutable insert-only observations;
keyset pages pin a high-water mark so concurrent inserts cannot move old pages.
Cursor keys are owner supplied (32+ bytes); never exposed to browser code.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from aitos.config.durable_files import exclusive_file_lock, prepare_durable_directory
from aitos.trading.account_reconciliation import amount, name, stamp

FIELDS = (
    "totalWalletBalance",
    "totalUnrealizedProfit",
    "totalMarginBalance",
    "totalInitialMargin",
    "totalMaintMargin",
    "availableBalance",
)


class AccountTimeline:
    def __init__(self, directory, *, account, cursor_key):
        self.account = name(account)
        if type(cursor_key) is not bytes or len(cursor_key) < 32:
            raise ValueError("owner supplied cursor signing key required")
        self._key = cursor_key
        self.directory = prepare_durable_directory(directory)
        self.path = self.directory / "account-timeline.sqlite3"
        if self.path.is_symlink():
            raise ValueError("owned timeline database required")
        with exclusive_file_lock(self.directory / "timeline.lock"):
            fd = (
                os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                if not self.path.exists()
                else None
            )
            if fd is not None:
                os.close(fd)
            with self._connect() as db:
                existing = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if existing and existing != {
                    "identity",
                    "observations",
                    "sqlite_sequence",
                }:
                    raise ValueError(
                        "unrecognized timeline database; no migration performed"
                    )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS identity(version INTEGER NOT NULL, account TEXT NOT NULL, store_id TEXT NOT NULL)"
                )
                identity = db.execute(
                    "SELECT version,account,store_id FROM identity"
                ).fetchall()
                if not identity:
                    store_id = secrets.token_hex(16)
                    db.execute(
                        "INSERT INTO identity VALUES (1,?,?)", (self.account, store_id)
                    )
                elif len(identity) != 1 or identity[0][:2] != (1, self.account):
                    raise ValueError("timeline account/schema mismatch")
                else:
                    store_id = identity[0][2]
                self._scope = hashlib.sha256(
                    (self.account + ":" + store_id).encode()
                ).hexdigest()
                db.execute(
                    "CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, totals TEXT NOT NULL)"
                )

                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS observations_no_{action.lower()} BEFORE {action} ON observations BEGIN SELECT RAISE(ABORT, 'observation history is append-only'); END"
                    )

    @contextlib.contextmanager
    def _connect(self):
        if self.path.is_symlink():
            raise ValueError("owned timeline database required")
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def append(self, observation):
        if (
            observation.get("source") != "binance_signed_rest"
            or observation.get("controls") != "read_only"
            or observation.get("schema_version") != 1
        ):
            raise ValueError("recognized provider observation required")
        observed = stamp(observation["observed_at"])
        if not -5 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 30:
            raise ValueError("fresh observation required")
        totals = observation["totals"]
        if type(totals) is not dict or set(totals) != set(FIELDS):
            raise ValueError("complete totals required")
        for value in totals.values():
            amount(value)
        # Only decimal totals and timestamp are persisted; no arbitrary provider
        # fields, credentials, account identifiers or position payloads copied.
        with exclusive_file_lock(self.directory / "timeline.lock"):
            with self._connect() as db:
                db.execute(
                    "INSERT INTO observations(observed_at,totals) VALUES (?,?)",
                    (observed.isoformat(), json.dumps(totals, allow_nan=False)),
                )

    def _encode(self, values):
        data = json.dumps([self._scope, *values], separators=(",", ":")).encode()
        signature = hmac.new(self._key, data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + data).decode().rstrip("=")

    def _decode(self, cursor):
        try:
            if type(cursor) is not str or len(cursor) > 512:
                raise ValueError()
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            signature, data = raw[:32], raw[32:]
            if not hmac.compare_digest(
                signature, hmac.new(self._key, data, hashlib.sha256).digest()
            ):
                raise ValueError()
            account, upper, before, limit = json.loads(data)
            if (
                account != self._scope
                or any(type(n) is not int for n in (upper, before, limit))
                or not 0 < before <= upper + 1
                or not 1 <= limit <= 100
            ):
                raise ValueError()
            return upper, before, limit
        except Exception:
            raise ValueError("invalid account timeline cursor") from None

    def page(self, *, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded page size required")
        with self._connect() as db:
            if cursor is None:
                upper = db.execute(
                    "SELECT coalesce(max(id),0) FROM observations"
                ).fetchone()[0]
                before = upper + 1
            else:
                upper, before, bound_limit = self._decode(cursor)
                if bound_limit != limit:
                    raise ValueError("cursor page size mismatch")
            rows = db.execute(
                "SELECT id,observed_at,totals FROM observations WHERE id<=? AND id<? ORDER BY id DESC LIMIT ?",
                (upper, before, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        selected = rows[:limit]
        return dict(
            schema_version=1,
            source="durable_account_observations",
            consistent_insert_cut=True,
            upper_id=upper,
            rows=[
                dict(id=i, observed_at=t, totals=json.loads(v)) for i, t, v in selected
            ],
            next_cursor=self._encode((upper, selected[-1][0], limit)) if more else None,
            limitations=[
                "recorded_observations_not_provider_atomic_cuts",
                "no_all_time_realized_pnl",
            ],
        )


class RecordedAccountObserver:
    """Record only successful reads; persistence failure fails the read closed."""

    def __init__(self, provider, timeline):
        self.provider, self.timeline = provider, timeline

    async def observe(self):
        result = await self.provider.observe()
        # A cancellation may happen after commit. Never retry a financial action:
        # these are observation inserts, not orders or authoritative events.
        await asyncio.to_thread(self.timeline.append, result)
        return result
