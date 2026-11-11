"""Durable single-host order-intent quarantine; never an exchange retry engine.

The pre-submit claim is committed before a venue side effect. Confirmed execution
remains unresolved until the lifecycle durably applies it. A restart cannot assume
that a missing response means no exposure. No credentials or raw HTTP bodies stored.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .order_executor import OrderRequest, OrderResult


class ExecutionUncertainError(RuntimeError):
    """Exposure may exist. Keep capital/protection ownership; do not blindly retry."""

    def __init__(self, symbol: str, client_order_id: str, reason: str) -> None:
        self.symbol = symbol
        self.client_order_id = client_order_id
        super().__init__(
            f"execution uncertain for {symbol}/{client_order_id}: {reason}"
        )


class ExecutionRecoveryRequired(ExecutionUncertainError):
    """Unapplied durable evidence blocks fresh exposure until reconciled."""


class ExecutionIntentStore:
    def __init__(self, path: str | Path, *, scope: str) -> None:
        self.path = Path(path)
        self.scope = scope
        if not scope:
            raise ValueError("execution intent scope is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS execution_intents (
                    scope TEXT NOT NULL, client_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, request_json TEXT NOT NULL,
                    state TEXT NOT NULL, result_json TEXT, trade_id TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(scope, client_id)
                )"""
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def begin(self, request: OrderRequest) -> None:
        if not request.client_order_id:
            raise ValueError("durable order intent requires client_order_id")
        payload = json.dumps(asdict(request), sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT request_json FROM execution_intents WHERE scope=? AND client_id=?",
                (self.scope, request.client_order_id),
            ).fetchone()
            if previous is not None:
                if previous["request_json"] != payload:
                    raise ValueError(
                        "client order ID reused for a different durable request"
                    )
                raise ExecutionRecoveryRequired(
                    request.symbol,
                    request.client_order_id,
                    "intent already exists; reconcile instead of resubmitting",
                )
            if (
                not request.reduce_only
                and conn.execute(
                    "SELECT 1 FROM execution_intents WHERE scope=? "
                    "AND state NOT IN ('APPLIED','REJECTED') LIMIT 1",
                    (self.scope,),
                ).fetchone()
            ):
                raise ExecutionRecoveryRequired(
                    request.symbol,
                    request.client_order_id,
                    "unresolved durable intent blocks new exposure",
                )
            conn.execute(
                "INSERT INTO execution_intents(scope,client_id,symbol,request_json,state) "
                "VALUES (?,?,?,?,'SUBMITTING')",
                (self.scope, request.client_order_id, request.symbol, payload),
            )

    def record_result(self, client_id: str, result: OrderResult) -> None:
        if result.success:
            if (
                not result.order_id
                or not math.isfinite(result.filled_quantity)
                or not math.isfinite(result.fill_price)
                or result.filled_quantity <= 0
                or result.fill_price <= 0
            ):
                raise ValueError(
                    "confirmed execution requires finite positive fill evidence"
                )
        elif result.filled_quantity != 0:
            raise ValueError(
                "execution with partial exposure cannot be recorded as rejected"
            )
        state = "CONFIRMED" if result.success else "REJECTED"
        payload = json.dumps(asdict(result), sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE execution_intents SET state=?,result_json=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE scope=? AND client_id=? AND state='SUBMITTING'",
                (state, payload, self.scope, client_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("execution result has no uncommitted intent")

    def record_protection(self, client_id: str, result: OrderResult) -> None:
        """Accepted resting protection is not a fill; retain until durable ID write."""
        if not result.success or not result.order_id or result.filled_quantity != 0:
            raise ValueError("protection acceptance requires an unfilled resting order")
        payload = json.dumps(asdict(result), sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_json FROM execution_intents WHERE scope=? AND client_id=? AND state='SUBMITTING'",
                (self.scope, client_id),
            ).fetchone()
            request = json.loads(row[0]) if row else {}
            if request.get("order_type") not in {
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
            } or not request.get("reduce_only"):
                raise ValueError("protection acceptance requires a protective preclaim")
            conn.execute(
                "UPDATE execution_intents SET state='PROTECTED',result_json=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE scope=? AND client_id=?",
                (payload, self.scope, client_id),
            )

    def mark_applied(self, client_id: str, trade_id: str) -> None:
        if not trade_id:
            raise ValueError("applied execution requires durable trade identity")
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE execution_intents SET state='APPLIED',trade_id=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE scope=? AND client_id=? AND state IN ('CONFIRMED','PROTECTED')",
                (trade_id, self.scope, client_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only confirmed execution can be applied")

    def unresolved(self, symbol: str | None = None) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT client_id,symbol,state,request_json,result_json FROM execution_intents "
                "WHERE scope=? AND state NOT IN ('APPLIED','REJECTED') ORDER BY client_id",
                (self.scope,),
            ).fetchall()
        return [dict(row) for row in rows if symbol is None or row["symbol"] == symbol]

    def assert_ready(self) -> None:
        rows = self.unresolved()
        if rows:
            row = rows[0]
            raise ExecutionRecoveryRequired(
                row["symbol"],
                row["client_id"],
                "startup requires reconciliation of durable intent",
            )
