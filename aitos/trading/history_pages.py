"""Opt-in durable materialization of bounded ledger query results.

Pagination is consistent within the retained result, not proof of a transactional
exchange/ledger cut or all-time completeness. No financial store is modified.
Expired files are retained for owner inspection; quota exhaustion fails closed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from aitos.trading.account_reconciliation import name

from aitos.config.durable_files import (
    prepare_durable_directory,
    exclusive_file_lock,
    atomic_write_json,
)


class HistoryPages:
    def __init__(
        self,
        state_store,
        directory,
        *,
        cursor_key,
        scope,
        clock=time.time,
        capture_limit=2000,
    ):
        if type(cursor_key) is not bytes or not 32 <= len(cursor_key) <= 128:
            raise ValueError("bounded owner cursor key required")
        if type(capture_limit) is not int or not 1 <= capture_limit <= 10000:
            raise ValueError("bounded materialization window required")
        self._scope = hashlib.sha256(name(scope).encode()).hexdigest()
        self.capture_limit = capture_limit
        self.source = state_store
        self.directory = prepare_durable_directory(directory)
        self._key, self._clock = cursor_key, clock
        self._lock = asyncio.Lock()

    def _encode(self, identity, offset, limit, digest):
        data = json.dumps(
            [identity, offset, limit, digest], separators=(",", ":")
        ).encode()
        signature = hmac.new(self._key, data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + data).decode().rstrip("=")

    def _decode(self, cursor, limit):
        try:
            if type(cursor) is not str or len(cursor) > 256:
                raise ValueError()
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            signature, data = raw[:32], raw[32:]
            if not hmac.compare_digest(
                signature, hmac.new(self._key, data, hashlib.sha256).digest()
            ):
                raise ValueError()
            identity, offset, bound_limit, digest = json.loads(data)
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[a-f0-9]{32}", identity)
                or type(offset) is not int
                or not 0 <= offset < 10000
                or type(bound_limit) is not int
                or bound_limit != limit
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                raise ValueError()
            return identity, offset, digest
        except Exception:
            raise ValueError("invalid history continuation") from None

    def _save(self, captured, limit):
        from aitos.console_api import public_trade

        if (
            type(captured) is not dict
            or set(captured) != {"rows", "truncated"}
            or type(captured["rows"]) is not list
            or len(captured["rows"]) > 10000
            or type(captured["truncated"]) is not bool
        ):
            raise RuntimeError("invalid bounded ledger capture")
        rows = [public_trade(row) for row in captured["rows"]]
        ids = [row["trade_id"] for row in rows]
        if any(not isinstance(i, str) or not 1 <= len(i) <= 128 for i in ids) or len(
            set(ids)
        ) != len(ids):
            raise RuntimeError("unique ledger identities required")
        value = dict(
            schema_version=1,
            scope=self._scope,
            created_at=self._clock(),
            rows=rows,
            truncated=captured["truncated"],
        )
        with exclusive_file_lock(self.directory / "history-pages.lock"):
            if len(list(self.directory.glob("*.json"))) >= 32:
                raise RuntimeError("history materialization quota exhausted")
            identity = secrets.token_hex(16)
            path = self.directory / (identity + ".json")
            if path.exists():
                raise RuntimeError("history materialization identity collision")
            atomic_write_json(path, value)
        return self._read(identity, 0, limit)

    def _read(self, identity, offset, limit, expected_digest=None):
        path = self.directory / (identity + ".json")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 4 * 1024 * 1024
        ):
            raise ValueError("history materialization unavailable")
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if expected_digest is not None and digest != expected_digest:
                raise ValueError("history materialization changed")
            value = json.loads(raw)
            if (
                value["schema_version"] != 1
                or value["scope"] != self._scope
                or not 0 <= self._clock() - value["created_at"] <= 900
            ):
                raise ValueError()
            rows = value["rows"]
            if (
                type(rows) is not list
                or len(rows) > 10000
                or (offset and offset >= len(rows))
            ):
                raise ValueError()
            page = rows[offset : offset + limit]
            next_offset = offset + len(page)
            return dict(
                schema_version=1,
                source="materialized_ledger_query",
                available=True,
                consistent_pages=True,
                snapshot_id=identity,
                captured_at=value["created_at"],
                expires_at=value["created_at"] + 900,
                rows=page,
                limit=limit,
                page_cursor=self._encode(identity, offset, limit, digest),
                next_cursor=(
                    self._encode(identity, next_offset, limit, digest)
                    if next_offset < len(rows)
                    else None
                ),
                truncated=value["truncated"],
                total_captured=len(rows),
                limitations=[
                    "bounded_materialized_query_not_all_time",
                    "source_query_not_exchange_atomic_cut",
                ],
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("invalid history materialization") from None

    async def page(self, *, limit=100, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded history page required")
        if cursor is not None:
            identity, offset, digest = self._decode(cursor, limit)
            return await asyncio.to_thread(self._read, identity, offset, limit, digest)
        if self._lock.locked():
            raise RuntimeError("history capture already pending")
        async with self._lock:
            captured = await asyncio.wait_for(
                self.source.query_closed_snapshot(limit=self.capture_limit), timeout=5
            )
            return await asyncio.to_thread(self._save, captured, limit)
