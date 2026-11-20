"""Durable single-host order-intent quarantine; never an exchange retry engine.

The pre-submit claim is committed before a venue side effect. Confirmed execution
remains unresolved until the lifecycle durably applies it. A restart cannot assume
that a missing response means no exposure. No credentials or raw HTTP bodies stored.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from decimal import Decimal, InvalidOperation
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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS protection_handovers (
                    scope TEXT NOT NULL,
                    handover_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    old_order_id TEXT NOT NULL,
                    new_client_id TEXT NOT NULL,
                    new_order_id TEXT,
                    state TEXT NOT NULL,
                    blocked_reason TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(scope, handover_id),
                    UNIQUE(scope, new_client_id)
                )"""
            )
            handover_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(protection_handovers)")
            }
            if "order_type" not in handover_columns:
                conn.execute(
                    "ALTER TABLE protection_handovers ADD COLUMN order_type TEXT "
                    "NOT NULL DEFAULT 'STOP_MARKET'"
                )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS protection_child_fills (
                    scope TEXT NOT NULL,
                    algo_id TEXT NOT NULL,
                    child_order_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    cumulative_quantity TEXT NOT NULL,
                    cumulative_quote TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(scope, algo_id, child_order_id)
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
                    "AND state NOT IN ('APPLIED','REJECTED','TERMINAL') LIMIT 1",
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
        if (
            not result.success
            or not isinstance(result.order_id, str)
            or not re.fullmatch(r"(?:algo:)?[1-9][0-9]{0,29}", result.order_id)
            or not math.isfinite(result.filled_quantity)
            or result.filled_quantity != 0
            or not math.isfinite(result.fill_price)
            or result.fill_price <= 0
            or result.client_order_id != client_id
        ):
            raise ValueError(
                "protection acceptance requires valid identity and "
                "an unfilled resting order"
            )
        payload = json.dumps(asdict(result), sort_keys=True, allow_nan=False)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT symbol, request_json FROM execution_intents "
                "WHERE scope=? AND client_id=? AND state='SUBMITTING'",
                (self.scope, client_id),
            ).fetchone()
            if row is None:
                raise ValueError("protection acceptance has no submitting preclaim")
            request = json.loads(row["request_json"])
            request_side = str(request.get("side", "")).upper()
            result_side = str(getattr(result.side, "value", result.side)).upper()
            if (
                request.get("order_type")
                not in {
                    "STOP_MARKET",
                    "TAKE_PROFIT_MARKET",
                }
                or request.get("reduce_only") is not True
            ):
                raise ValueError("protection acceptance requires a protective preclaim")
            if (
                request.get("symbol") != row["symbol"]
                or result.symbol != row["symbol"]
                or result_side != request_side
                or not math.isclose(
                    result.fill_price,
                    float(request.get("reference_price", 0)),
                    rel_tol=1e-12,
                    abs_tol=0.0,
                )
            ):
                raise ValueError(
                    "protection result does not match preclaim identity "
                    "or trigger price"
                )
            cursor = conn.execute(
                "UPDATE execution_intents SET state='PROTECTED',result_json=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE scope=? AND client_id=? AND state='SUBMITTING'",
                (payload, self.scope, client_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "protection preclaim changed before durable acceptance"
                )

    def begin_protection_handover(
        self,
        *,
        handover_id: str,
        trade_id: str,
        symbol: str,
        old_order_id: str,
        new_client_id: str,
        order_type: str = "STOP_MARKET",
    ) -> dict:
        """Durably claim a replacement before its venue submission.

        Replays are accepted only when every identity matches. A pending or
        blocked handover for the same trade prevents a second replacement.
        """
        values = (handover_id, trade_id, symbol, old_order_id, new_client_id)
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("unsupported protective handover order type")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("complete protective handover identity is required")
        if old_order_id == new_client_id:
            raise ValueError("old and new protective identities must differ")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", handover_id):
            raise ValueError("invalid protective handover identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", new_client_id):
            raise ValueError("invalid replacement client identity")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM protection_handovers WHERE scope=? AND handover_id=?",
                (self.scope, handover_id),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                expected = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "old_order_id": old_order_id,
                    "new_client_id": new_client_id,
                    "order_type": order_type,
                }
                if any(row[key] != value for key, value in expected.items()):
                    raise ValueError(
                        "protective handover ID reused for different identity"
                    )
                return row
            active = conn.execute(
                "SELECT handover_id FROM protection_handovers WHERE scope=? "
                "AND trade_id=? AND state NOT IN ('APPLIED') LIMIT 1",
                (self.scope, trade_id),
            ).fetchone()
            if active is not None:
                raise ExecutionRecoveryRequired(
                    symbol,
                    str(active["handover_id"]),
                    "another protective handover requires reconciliation",
                )
            conn.execute(
                "INSERT INTO protection_handovers(scope,handover_id,trade_id,symbol,"
                "old_order_id,new_client_id,order_type,state) "
                "VALUES (?,?,?,?,?,?,?,'PREPARED')",
                (
                    self.scope,
                    handover_id,
                    trade_id,
                    symbol,
                    old_order_id,
                    new_client_id,
                    order_type,
                ),
            )
            row = conn.execute(
                "SELECT * FROM protection_handovers WHERE scope=? AND handover_id=?",
                (self.scope, handover_id),
            ).fetchone()
            return dict(row)

    def record_handover_new_order(self, handover_id: str, new_order_id: str) -> None:
        if not re.fullmatch(r"algo:[1-9][0-9]{0,29}", str(new_order_id or "")):
            raise ValueError("valid replacement algo order ID required")
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE protection_handovers SET new_order_id=?,state='NEW_ACCEPTED',"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND handover_id=? "
                "AND state IN ('PREPARED','NEW_ACCEPTED') "
                "AND (new_order_id IS NULL OR new_order_id=?)",
                (new_order_id, self.scope, handover_id, new_order_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("replacement acceptance conflicts with handover state")

    def mark_handover_old_cancel_requested(self, handover_id: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE protection_handovers SET state='OLD_CANCEL_REQUESTED',"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND handover_id=? "
                "AND state IN ('NEW_ACCEPTED','OLD_CANCEL_REQUESTED') "
                "AND new_order_id IS NOT NULL",
                (self.scope, handover_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("old-stop cancellation requires accepted replacement")

    def mark_handover_old_cancelled(self, handover_id: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE protection_handovers SET state='OLD_CANCELLED',"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND handover_id=? "
                "AND state IN ('OLD_CANCEL_REQUESTED','OLD_CANCELLED')",
                (self.scope, handover_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("old-stop cancellation was not durably requested")

    def complete_protection_handover(self, handover_id: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE protection_handovers SET state='APPLIED',"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND handover_id=? "
                "AND state IN ('OLD_CANCELLED','APPLIED')",
                (self.scope, handover_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "handover cannot complete before old stop cancellation"
                )

    def block_protection_handover(self, handover_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("handover quarantine reason is required")
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE protection_handovers SET state='BLOCKED',blocked_reason=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND handover_id=? "
                "AND state!='APPLIED'",
                (reason[:500], self.scope, handover_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("handover cannot be quarantined")

    def protection_handovers(self, *, trade_id: str | None = None) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM protection_handovers WHERE scope=? "
                "ORDER BY updated_at,handover_id",
                (self.scope,),
            ).fetchall()
        return [
            dict(row) for row in rows if trade_id is None or row["trade_id"] == trade_id
        ]

    def protection_child_watermark(
        self, algo_id: str, child_order_id: str
    ) -> dict | None:
        """Return the last applied child cumulative watermark for this account."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM protection_child_fills WHERE scope=? AND algo_id=? "
                "AND child_order_id=?",
                (self.scope, str(algo_id), str(child_order_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_protection_child_applied(
        self,
        *,
        algo_id: str,
        child_order_id: str,
        trade_id: str,
        symbol: str,
        cumulative_quantity: str,
        cumulative_quote: str,
        status: str,
    ) -> dict:
        """Advance the SQLite mirror only after the durable trade snapshot write.

        The trade snapshot's per-child marker remains the replay authority across
        a crash between the ClickHouse write and this journal acknowledgement.
        """
        identities = (algo_id, child_order_id, trade_id, symbol, status)
        if any(not isinstance(value, str) or not value.strip() for value in identities):
            raise ValueError("complete child-fill application identity is required")
        try:
            quantity = Decimal(str(cumulative_quantity))
            quote = Decimal(str(cumulative_quote))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("child cumulative values must be decimals") from None
        if (
            not quantity.is_finite()
            or not quote.is_finite()
            or quantity < 0
            or quote < 0
        ):
            raise ValueError("child cumulative values must be finite and non-negative")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM protection_child_fills WHERE scope=? AND algo_id=? "
                "AND child_order_id=?",
                (self.scope, algo_id, child_order_id),
            ).fetchone()
            if row is not None:
                current = dict(row)
                if (
                    current["trade_id"] != trade_id
                    or current["symbol"] != symbol
                ):
                    raise ValueError(
                        "child-fill identity was reused across trade scope"
                    )
                previous_quantity = Decimal(current["cumulative_quantity"])
                previous_quote = Decimal(current["cumulative_quote"])
                if quantity < previous_quantity or quote < previous_quote:
                    raise ValueError("child-fill cumulative watermark regressed")
                if (quantity == previous_quantity) != (quote == previous_quote):
                    raise ValueError("child-fill quantity/quote watermark diverged")
                if quantity == previous_quantity:
                    conn.execute(
                        "UPDATE protection_child_fills SET status=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND algo_id=? "
                        "AND child_order_id=?",
                        (status, self.scope, algo_id, child_order_id),
                    )
                    current["status"] = status
                    return current
                conn.execute(
                    "UPDATE protection_child_fills SET cumulative_quantity=?,"
                    "cumulative_quote=?,status=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE scope=? AND algo_id=? AND child_order_id=?",
                    (
                        str(quantity),
                        str(quote),
                        status,
                        self.scope,
                        algo_id,
                        child_order_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO protection_child_fills(scope,algo_id,child_order_id,"
                    "trade_id,symbol,cumulative_quantity,cumulative_quote,status) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        self.scope,
                        algo_id,
                        child_order_id,
                        trade_id,
                        symbol,
                        str(quantity),
                        str(quote),
                        status,
                    ),
                )
            result = conn.execute(
                "SELECT * FROM protection_child_fills WHERE scope=? AND algo_id=? "
                "AND child_order_id=?",
                (self.scope, algo_id, child_order_id),
            ).fetchone()
            return dict(result)

    def mark_protection_terminal(
        self,
        order_id: str,
        *,
        symbol: str,
        status: str,
        executed_quantity: float = 0.0,
    ) -> bool:
        """Persist confirmed terminal venue evidence for a protective order."""
        terminal_states = {
            "CANCELED",
            "FILLED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
            "REJECTED",
        }
        if (
            not order_id
            or not symbol
            or status not in terminal_states
            or not math.isfinite(executed_quantity)
            or executed_quantity < 0
        ):
            raise ValueError("valid terminal protective-order evidence required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT client_id,result_json,state FROM execution_intents "
                "WHERE scope=? AND symbol=? AND state IN ('PROTECTED','APPLIED')",
                (self.scope, symbol),
            ).fetchall()
            matches = []
            for row in rows:
                result = json.loads(row["result_json"] or "{}")
                if result.get("order_id") == order_id:
                    matches.append((row["client_id"], result))
            if len(matches) > 1:
                raise ValueError("protective venue order identity is ambiguous")
            if not matches:
                return False
            client_id, result = matches[0]
            result["terminal_status"] = status
            result["terminal_executed_quantity"] = executed_quantity
            payload = json.dumps(result, sort_keys=True, allow_nan=False)
            cursor = conn.execute(
                "UPDATE execution_intents SET state='TERMINAL',result_json=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE scope=? AND client_id=? "
                "AND state IN ('PROTECTED','APPLIED')",
                (payload, self.scope, client_id),
            )
            return cursor.rowcount == 1

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
                "WHERE scope=? AND state NOT IN ('APPLIED','REJECTED','TERMINAL') ORDER BY client_id",
                (self.scope,),
            ).fetchall()
        return [dict(row) for row in rows if symbol is None or row["symbol"] == symbol]

    def protection_intents(self) -> list[dict]:
        """Read all non-rejected protective intent states for audit only."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT client_id,symbol,state,request_json,result_json,trade_id "
                "FROM execution_intents WHERE scope=? "
                "AND state IN ('SUBMITTING','PROTECTED','APPLIED','TERMINAL') "
                "ORDER BY client_id",
                (self.scope,),
            ).fetchall()
        return [dict(row) for row in rows]

    def assert_ready(self) -> None:
        rows = self.unresolved()
        if rows:
            row = rows[0]
            raise ExecutionRecoveryRequired(
                row["symbol"],
                row["client_id"],
                "startup requires reconciliation of durable intent",
            )
