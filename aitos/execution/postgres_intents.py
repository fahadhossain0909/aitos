"""PostgreSQL-backed execution intents sharing the canonical OMS transaction.

This adapter is async and deliberately separate from the legacy synchronous
SQLite journal. Runtime adoption must be explicit; it never silently dual-writes.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from aitos.execution.postgres_oms import (
    OMSConflict,
    OMSIntegrityError,
    WriterFence,
    _canonical_payload,
    _event_hash,
    _identifier,
)


def _financial_state_snapshot(
    snapshot: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, str]:
    """Validate accounting inputs and derive equity and path-dependent drawdown."""
    required = {
        "currency",
        "starting_equity",
        "realized_pnl",
        "unrealized_pnl",
        "fees",
        "funding",
    }
    allowed = required | {"venue_equity"}
    if (
        not isinstance(snapshot, dict)
        or not required.issubset(snapshot)
        or not set(snapshot).issubset(allowed)
    ):
        raise ValueError(
            "financial snapshot must contain currency, starting_equity, "
            "realized_pnl, unrealized_pnl, fees and funding, with optional venue_equity"
        )
    currency = _identifier(str(snapshot.get("currency") or ""), "currency")
    if not 2 <= len(currency) <= 12 or not all(
        "A" <= char <= "Z" or "0" <= char <= "9" for char in currency
    ):
        raise ValueError("financial snapshot currency must be an uppercase code")

    def amount(name: str, *, nonnegative: bool = False) -> Decimal:
        raw = snapshot[name]
        if isinstance(raw, bool):
            raise ValueError(f"financial {name} must be a decimal amount")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"financial {name} must be a decimal amount") from None
        if not value.is_finite() or (nonnegative and value < 0):
            raise ValueError(f"financial {name} is non-finite or negative")
        normalized = value.normalize()
        scale = max(0, -normalized.as_tuple().exponent)
        integer_digits = max(1, normalized.adjusted() + 1) if value else 1
        if scale > 18 or integer_digits > 20:
            raise ValueError(f"financial {name} exceeds NUMERIC(38,18)")
        return value

    with localcontext() as context:
        context.prec = 60
        starting = amount("starting_equity", nonnegative=True)
        realized = amount("realized_pnl")
        unrealized = amount("unrealized_pnl")
        fees = amount("fees", nonnegative=True)
        funding = amount("funding")
        model_equity = starting + realized + unrealized + funding - fees
        venue_equity = (
            amount("venue_equity") if "venue_equity" in snapshot else model_equity
        )
        unattributed_adjustments = venue_equity - model_equity
        equity = venue_equity
        if previous is not None:
            if previous.get("currency") != currency:
                raise ValueError("financial currency cannot change within an aggregate")
            previous_starting = Decimal(str(previous["starting_equity"]))
            if previous_starting != starting:
                raise ValueError(
                    "starting equity is immutable within a currency ledger"
                )
            previous_peak = Decimal(str(previous["peak_equity"]))
            previous_max_drawdown = Decimal(str(previous["max_drawdown"]))
            previous_max_drawdown_pct = Decimal(str(previous["max_drawdown_pct"]))
        else:
            previous_peak = starting
            previous_max_drawdown = Decimal(0)
            previous_max_drawdown_pct = Decimal(0)
        peak = max(previous_peak, equity)
        drawdown = max(Decimal(0), peak - equity)
        drawdown_pct = drawdown / peak * Decimal(100) if peak > 0 else Decimal(0)
        max_drawdown = max(previous_max_drawdown, drawdown)
        max_drawdown_pct = max(previous_max_drawdown_pct, drawdown_pct)

    def canonical_amount(value: Decimal) -> str:
        if value == 0:
            return "0"
        encoded = format(value, "f")
        return encoded.rstrip("0").rstrip(".") if "." in encoded else encoded

    return {
        "currency": currency,
        "starting_equity": canonical_amount(starting),
        "realized_pnl": canonical_amount(realized),
        "unrealized_pnl": canonical_amount(unrealized),
        "fees": canonical_amount(fees),
        "funding": canonical_amount(funding),
        "venue_equity": canonical_amount(venue_equity),
        "unattributed_adjustments": canonical_amount(unattributed_adjustments),
        "equity": canonical_amount(equity),
        "peak_equity": canonical_amount(peak),
        "drawdown": canonical_amount(drawdown),
        "drawdown_pct": canonical_amount(drawdown_pct),
        "max_drawdown": canonical_amount(max_drawdown),
        "max_drawdown_pct": canonical_amount(max_drawdown_pct),
    }


_ALLOWED_TRANSITIONS = {
    "SUBMITTING": {"CONFIRMED", "REJECTED", "PROTECTED"},
    "CONFIRMED": {"APPLIED", "TERMINAL"},
    "PROTECTED": {"APPLIED", "TERMINAL"},
    "APPLIED": {"TERMINAL"},
    "REJECTED": set(),
    "TERMINAL": set(),
}


class PostgresCanonicalExecutionIntentStore:
    """Fenced, revisioned intent state committed with canonical OMS events/outbox.

    Every mutation writes the intent row, append-only OMS event, aggregate head,
    and projection outbox in one PostgreSQL transaction. It does not submit or
    retry venue orders.
    """

    def __init__(self, oms: Any, fence: WriterFence) -> None:
        from aitos.execution.postgres_oms import PostgresCanonicalOMS

        if not isinstance(oms, PostgresCanonicalOMS):
            raise TypeError("PostgresCanonicalOMS is required")
        if type(fence) is not WriterFence:
            raise TypeError("server-issued writer fence is required")
        self.oms = oms
        self.fence = fence
        self.scope = fence.account_scope
        self._protection_journal = None

    async def _append_protection_event(
        self,
        conn: Any,
        aggregate_id: str,
        event_prefix: str,
        payload: dict[str, Any],
    ) -> tuple[int, str]:
        head = await conn.fetchrow(
            """SELECT revision FROM aitos_oms_aggregate_heads
               WHERE account_scope=$1 AND aggregate_id=$2""",
            self.scope,
            aggregate_id,
        )
        expected = int(head["revision"]) if head is not None else 0
        revision = expected + 1
        event_id = f"{event_prefix}:revision:{revision}"
        if len(event_id) > 192:
            raise ValueError("protection event identity exceeds canonical OMS limit")
        await self._append_event(
            conn,
            aggregate_id,
            event_id,
            expected,
            _canonical_payload(payload),
        )
        return revision, event_id

    @staticmethod
    def _handover_payload(row: Any) -> dict[str, Any]:
        return {
            "handover_id": row["handover_id"],
            "trade_id": row["trade_id"],
            "symbol": row["symbol"],
            "old_order_id": row["old_order_id"],
            "new_client_id": row["new_client_id"],
            "new_order_id": row["new_order_id"],
            "order_type": row["order_type"],
            "state": row["state"],
            "blocked_reason": row["blocked_reason"],
        }

    async def begin_protection_handover(
        self,
        *,
        handover_id: str,
        trade_id: str,
        symbol: str,
        old_order_id: str,
        new_client_id: str,
        order_type: str = "STOP_MARKET",
    ) -> dict[str, Any]:
        """Create a fenced replacement claim before any venue POST."""
        values = (handover_id, trade_id, symbol, old_order_id, new_client_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("complete protective handover identity is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", handover_id):
            raise ValueError("invalid protective handover identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", new_client_id):
            raise ValueError("invalid replacement client identity")
        if old_order_id == new_client_id:
            raise ValueError("old and new protective identities must differ")
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("unsupported protective handover order type")
        scope = self.scope
        trade = _identifier(trade_id, "trade_id")
        symbol_value = _identifier(symbol, "symbol")
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            existing = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND handover_id=$2 FOR UPDATE""",
                scope,
                handover_id,
            )
            expected = {
                "trade_id": trade,
                "symbol": symbol_value,
                "old_order_id": old_order_id,
                "new_client_id": new_client_id,
                "order_type": order_type,
            }
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise OMSConflict(
                        "protective handover ID reused for different identity"
                    )
                return dict(existing)
            active = await conn.fetchrow(
                """SELECT handover_id FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND trade_id=$2 AND state<>'APPLIED'
                   LIMIT 1""",
                scope,
                trade,
            )
            if active is not None:
                raise OMSConflict(
                    "another protective handover requires reconciliation: "
                    + str(active["handover_id"])
                )
            await conn.execute(
                """INSERT INTO aitos_oms_protection_handovers
                   (account_scope,handover_id,trade_id,symbol,old_order_id,
                    new_client_id,order_type,state)
                   VALUES($1,$2,$3,$4,$5,$6,$7,'PREPARED')""",
                scope,
                handover_id,
                trade,
                symbol_value,
                old_order_id,
                new_client_id,
                order_type,
            )
            row = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND handover_id=$2""",
                scope,
                handover_id,
            )
            await self._append_protection_event(
                conn,
                f"protection-handover-{handover_id}",
                f"protection-handover:{handover_id}",
                {
                    "event_type": "PROTECTION_HANDOVER_CREATED",
                    "account_scope": scope,
                    **self._handover_payload(row),
                },
            )
            return dict(row)

    async def _transition_protection_handover(
        self,
        handover_id: str,
        *,
        allowed_states: set[str],
        new_state: str,
        event_type: str,
        new_order_id: str | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        handover = _identifier(handover_id, "handover_id")
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            row = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND handover_id=$2 FOR UPDATE""",
                self.scope,
                handover,
            )
            if row is None:
                raise OMSConflict("protective handover does not exist")
            if row["state"] == new_state:
                if new_order_id is not None and row["new_order_id"] != new_order_id:
                    raise OMSConflict("protective handover order identity changed")
                if (
                    blocked_reason is not None
                    and row["blocked_reason"] != blocked_reason
                ):
                    raise OMSConflict("protective handover quarantine reason changed")
                return dict(row)
            if row["state"] not in allowed_states:
                raise OMSConflict(
                    f"invalid protection handover transition {row['state']}->{new_state}"
                )
            if new_order_id is not None and row["new_order_id"] not in (
                None,
                new_order_id,
            ):
                raise OMSConflict(
                    "replacement venue order ID conflicts with durable claim"
                )
            if new_state == "OLD_CANCEL_REQUESTED" and not row["new_order_id"]:
                raise OMSConflict("old-stop cancellation requires accepted replacement")
            if new_state == "OLD_CANCELLED" and row["state"] != "OLD_CANCEL_REQUESTED":
                raise OMSConflict("old-stop cancellation was not durably requested")
            if new_state == "APPLIED" and row["state"] != "OLD_CANCELLED":
                raise OMSConflict(
                    "handover cannot complete before old stop cancellation"
                )
            await conn.execute(
                """UPDATE aitos_oms_protection_handovers
                   SET state=$3,new_order_id=COALESCE($4,new_order_id),
                       blocked_reason=COALESCE($5,blocked_reason),updated_at=now()
                   WHERE account_scope=$1 AND handover_id=$2""",
                self.scope,
                handover,
                new_state,
                new_order_id,
                blocked_reason[:500] if blocked_reason is not None else None,
            )
            updated = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND handover_id=$2""",
                self.scope,
                handover,
            )
            await self._append_protection_event(
                conn,
                f"protection-handover-{handover}",
                f"protection-handover:{handover}",
                {
                    "event_type": event_type,
                    "account_scope": self.scope,
                    **self._handover_payload(updated),
                },
            )
            return dict(updated)

    async def record_handover_new_order(
        self, handover_id: str, new_order_id: str
    ) -> dict[str, Any]:
        if not re.fullmatch(r"algo:[1-9][0-9]{0,29}", str(new_order_id or "")):
            raise ValueError("valid replacement algo order ID required")
        return await self._transition_protection_handover(
            handover_id,
            allowed_states={"PREPARED"},
            new_state="NEW_ACCEPTED",
            event_type="PROTECTION_HANDOVER_NEW_ORDER_ACCEPTED",
            new_order_id=new_order_id,
        )

    async def mark_handover_old_cancel_requested(
        self, handover_id: str
    ) -> dict[str, Any]:
        return await self._transition_protection_handover(
            handover_id,
            allowed_states={"NEW_ACCEPTED"},
            new_state="OLD_CANCEL_REQUESTED",
            event_type="PROTECTION_HANDOVER_OLD_CANCEL_REQUESTED",
        )

    async def mark_handover_old_cancelled(self, handover_id: str) -> dict[str, Any]:
        return await self._transition_protection_handover(
            handover_id,
            allowed_states={"OLD_CANCEL_REQUESTED"},
            new_state="OLD_CANCELLED",
            event_type="PROTECTION_HANDOVER_OLD_CANCELLED",
        )

    async def complete_protection_handover(self, handover_id: str) -> dict[str, Any]:
        return await self._transition_protection_handover(
            handover_id,
            allowed_states={"OLD_CANCELLED"},
            new_state="APPLIED",
            event_type="PROTECTION_HANDOVER_COMPLETED",
        )

    async def block_protection_handover(
        self, handover_id: str, reason: str
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("handover quarantine reason is required")
        return await self._transition_protection_handover(
            handover_id,
            allowed_states={
                "PREPARED",
                "NEW_ACCEPTED",
                "OLD_CANCEL_REQUESTED",
                "OLD_CANCELLED",
                "BLOCKED",
            },
            new_state="BLOCKED",
            event_type="PROTECTION_HANDOVER_BLOCKED",
            blocked_reason=reason,
        )

    async def protection_handovers(
        self, *, trade_id: str | None = None
    ) -> list[dict[str, Any]]:
        if trade_id is not None:
            trade_id = _identifier(trade_id, "trade_id")
        async with self.oms._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM aitos_oms_protection_handovers
                   WHERE account_scope=$1 AND ($2::text IS NULL OR trade_id=$2)
                   ORDER BY updated_at,handover_id""",
                self.scope,
                trade_id,
            )
        return [dict(row) for row in rows]

    async def protection_child_watermark(
        self, algo_id: str, child_order_id: str
    ) -> dict[str, Any] | None:
        algo = _identifier(str(algo_id), "algo_id")
        child = _identifier(str(child_order_id), "child_order_id")
        async with self.oms._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT account_scope,algo_id,child_order_id,trade_id,symbol,
                          cumulative_quantity,cumulative_quote,status,updated_at
                   FROM aitos_oms_protection_child_fills
                   WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3""",
                self.scope,
                algo,
                child,
            )
        return dict(row) if row is not None else None

    async def mark_protection_child_applied(
        self,
        *,
        algo_id: str,
        child_order_id: str,
        trade_id: str,
        symbol: str,
        cumulative_quantity: str,
        cumulative_quote: str,
        status: str,
    ) -> dict[str, Any]:
        identities = (algo_id, child_order_id, trade_id, symbol, status)
        if any(not isinstance(value, str) or not value.strip() for value in identities):
            raise ValueError("complete child-fill application identity is required")
        algo = _identifier(algo_id, "algo_id")
        child = _identifier(child_order_id, "child_order_id")
        trade = _identifier(trade_id, "trade_id")
        symbol_value = _identifier(symbol, "symbol")
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
            or quantity.as_tuple().exponent < -18
            or quote.as_tuple().exponent < -18
        ):
            raise ValueError(
                "child cumulative values must be finite, non-negative and <=18 decimals"
            )
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            current = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_child_fills
                   WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3
                   FOR UPDATE""",
                self.scope,
                algo,
                child,
            )
            if current is not None:
                if current["trade_id"] != trade or current["symbol"] != symbol_value:
                    raise OMSConflict(
                        "child-fill identity was reused across trade scope"
                    )
                previous_quantity = Decimal(str(current["cumulative_quantity"]))
                previous_quote = Decimal(str(current["cumulative_quote"]))
                if quantity < previous_quantity or quote < previous_quote:
                    raise OMSConflict("child-fill cumulative watermark regressed")
                if (quantity == previous_quantity) != (quote == previous_quote):
                    raise OMSConflict("child-fill quantity/quote watermark diverged")
                if (
                    quantity == previous_quantity
                    and quote == previous_quote
                    and current["status"] == status
                ):
                    return dict(current)
                await conn.execute(
                    """UPDATE aitos_oms_protection_child_fills
                       SET cumulative_quantity=$4,cumulative_quote=$5,status=$6,updated_at=now()
                       WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3""",
                    self.scope,
                    algo,
                    child,
                    quantity,
                    quote,
                    status,
                )
            else:
                await conn.execute(
                    """INSERT INTO aitos_oms_protection_child_fills
                       (account_scope,algo_id,child_order_id,trade_id,symbol,
                        cumulative_quantity,cumulative_quote,status)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                    self.scope,
                    algo,
                    child,
                    trade,
                    symbol_value,
                    quantity,
                    quote,
                    status,
                )
            updated = await conn.fetchrow(
                """SELECT * FROM aitos_oms_protection_child_fills
                   WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3""",
                self.scope,
                algo,
                child,
            )
            aggregate = f"protection-child-{algo}-{child}"
            await self._append_protection_event(
                conn,
                aggregate,
                f"protection-child:{algo}:{child}",
                {
                    "event_type": "PROTECTION_CHILD_FILL_WATERMARK_UPDATED",
                    "account_scope": self.scope,
                    "algo_id": algo,
                    "child_order_id": child,
                    "trade_id": trade,
                    "symbol": symbol_value,
                    "cumulative_quantity": format(quantity, "f"),
                    "cumulative_quote": format(quote, "f"),
                    "status": status,
                },
            )
            return dict(updated)

    async def protection_intents(
        self, trade_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.oms._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT client_id,symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1
                     AND request->>'order_type' IN ('STOP_MARKET','TAKE_PROFIT_MARKET')
                     AND ($2::text IS NULL OR trade_id=$2 OR request->>'owner_trade_id'=$2)
                   ORDER BY client_id""",
                self.scope,
                trade_id,
            )
        return [dict(row) for row in rows]

    async def protection_intent_for_order(
        self, order_id: str, trade_id: str
    ) -> dict[str, Any] | None:
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("protective order ID is required")
        trade = _identifier(trade_id, "trade_id")
        async with self.oms._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT client_id,symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND trade_id=$2
                     AND request->>'order_type' IN ('STOP_MARKET','TAKE_PROFIT_MARKET')
                     AND (result->>'order_id'=$3 OR client_id=$3)
                   ORDER BY revision DESC LIMIT 1""",
                self.scope,
                trade,
                order_id,
            )
        if row is None:
            return None
        decoded = dict(row)
        decoded["request"] = json.loads(decoded["request_json"])
        decoded["result"] = (
            json.loads(decoded["result_json"]) if decoded.get("result_json") else None
        )
        return decoded

    async def mark_protection_terminal(
        self,
        order_id: str,
        *,
        symbol: str,
        status: str,
        executed_quantity: float,
    ) -> dict[str, Any]:
        terminal_states = {
            "CANCELED",
            "FILLED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
            "REJECTED",
        }
        if (
            not isinstance(order_id, str)
            or not order_id
            or not isinstance(symbol, str)
            or not symbol
            or status not in terminal_states
            or isinstance(executed_quantity, bool)
            or not isinstance(executed_quantity, (int, float))
            or not Decimal(str(executed_quantity)).is_finite()
            or executed_quantity < 0
        ):
            raise ValueError("valid terminal protective-order evidence is required")
        rows = await self.protection_intents()
        matches = []
        for row in rows:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            if row["symbol"] == symbol and order_id in {
                str(result.get("order_id", "")),
                str(row["client_id"]),
            }:
                matches.append(row)
        if len(matches) != 1:
            raise OMSConflict(
                "terminal protection order does not map to one canonical intent"
            )
        row = matches[0]
        current = await self.get(str(row["client_id"]))
        if current is None:
            raise OMSConflict("terminal protection intent disappeared")
        if current["state"] == "TERMINAL":
            return current
        result = (
            json.loads(current["result"])
            if isinstance(current.get("result"), str)
            else current.get("result") or {}
        )
        result = dict(result)
        result["terminal_status"] = status
        result["terminal_executed_quantity"] = format(
            Decimal(str(executed_quantity)), "f"
        )
        return await self.transition(
            str(row["client_id"]),
            expected_revision=int(current["revision"]),
            expected_state=str(current["state"]),
            new_state="TERMINAL",
            result=result,
        )

    async def mark_applied(self, client_id: str, trade_id: str) -> dict[str, Any]:
        """Compatibility recovery transition; normal writes use atomic Trade finalization."""
        row = await self.get(client_id)
        if row is None:
            raise OMSConflict("execution intent does not exist")
        if row["state"] == "APPLIED" and row["trade_id"] == trade_id:
            return row
        if row["state"] not in {"CONFIRMED", "PROTECTED"}:
            raise OMSConflict(
                "execution intent cannot be acknowledged from current state"
            )
        return await self.transition(
            client_id,
            expected_revision=int(row["revision"]),
            expected_state=str(row["state"]),
            new_state="APPLIED",
            trade_id=trade_id,
        )

    async def begin(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or not request:
            raise ValueError("non-empty execution request is required")
        payload = json.dumps(
            request, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        client_id = _identifier(str(request.get("client_order_id") or ""), "client_id")
        if len(client_id) > 180:
            raise ValueError("client_order_id exceeds canonical intent limit")
        symbol = _identifier(str(request.get("symbol") or ""), "symbol")
        aggregate = f"intent-{client_id}"
        event_id = f"intent:{client_id}:revision:1"
        event_payload = {
            "event_type": "EXECUTION_INTENT_CREATED",
            "client_order_id": client_id,
            "state": "SUBMITTING",
            "request": request,
        }
        await self._assert_fence()
        encoded = _canonical_payload(event_payload)
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            existing = await conn.fetchrow(
                """SELECT request::text AS request_json FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2 FOR UPDATE""",
                self.scope,
                client_id,
            )
            if existing is not None:
                if json.loads(existing["request_json"]) != json.loads(payload):
                    raise OMSConflict("client order ID reused with different request")
                raise OMSConflict(
                    "execution intent already exists; reconcile, do not resubmit"
                )
            await self._append_event(conn, aggregate, event_id, 0, encoded)
            await conn.execute(
                """INSERT INTO aitos_oms_execution_intents
                   (account_scope,client_id,symbol,state,request,result,trade_id,
                    revision,last_event_id)
                   VALUES($1,$2,$3,'SUBMITTING',$4::jsonb,NULL,NULL,1,$5)""",
                self.scope,
                client_id,
                symbol,
                payload,
                event_id,
            )
        return {
            "account_scope": self.scope,
            "client_id": client_id,
            "symbol": symbol,
            "state": "SUBMITTING",
            "request": request,
            "result": None,
            "trade_id": None,
            "revision": 1,
            "last_event_id": event_id,
        }

    async def transition(
        self,
        client_id: str,
        *,
        expected_revision: int,
        expected_state: str,
        new_state: str,
        result: dict[str, Any] | None = None,
        trade_id: str | None = None,
    ) -> dict[str, Any]:
        client = _identifier(client_id, "client_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("positive expected_revision is required")
        if new_state not in _ALLOWED_TRANSITIONS.get(expected_state, set()):
            raise ValueError(
                f"invalid execution-intent transition {expected_state}->{new_state}"
            )
        if result is not None and not isinstance(result, dict):
            raise TypeError("execution result must be an object")
        if new_state in {"CONFIRMED", "PROTECTED", "REJECTED"}:
            if result is None:
                raise ValueError(f"{new_state} intent requires result evidence")
            order_id = result.get("order_id")
            if new_state in {"CONFIRMED", "PROTECTED"} and (
                not isinstance(order_id, str) or not order_id
            ):
                raise ValueError(f"{new_state} intent requires a venue order_id")
            try:
                filled = Decimal(str(result.get("filled_quantity", "0")))
                fill_price = Decimal(str(result.get("fill_price", "0")))
            except (InvalidOperation, ValueError):
                raise ValueError("execution fill evidence must be decimal") from None
            if not filled.is_finite() or not fill_price.is_finite():
                raise ValueError("execution fill evidence must be finite")
            if new_state == "CONFIRMED" and (filled <= 0 or fill_price <= 0):
                raise ValueError("confirmed execution requires positive fill evidence")
            if new_state == "PROTECTED" and (filled != 0 or fill_price <= 0):
                raise ValueError(
                    "protective acceptance must be unfilled with a positive trigger"
                )
            if new_state == "REJECTED" and filled != 0:
                raise ValueError("rejected execution cannot contain a partial fill")
        if trade_id is not None:
            trade_id = _identifier(trade_id, "trade_id")
        aggregate = f"intent-{client}"
        event_id = f"intent:{client}:revision:{expected_revision + 1}"
        await self._assert_fence()
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            row = await conn.fetchrow(
                """SELECT symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2 FOR UPDATE""",
                self.scope,
                client,
            )
            if row is None:
                raise OMSConflict("execution intent does not exist")
            if (
                int(row["revision"]) != expected_revision
                or row["state"] != expected_state
            ):
                raise OMSConflict("execution intent revision/state CAS failed")
            if new_state == "APPLIED" and not trade_id:
                raise ValueError("APPLIED intent requires durable trade_id")
            if new_state in {"CONFIRMED", "PROTECTED"} and result is None:
                raise ValueError(f"{new_state} intent requires durable result evidence")
            next_revision = expected_revision + 1
            payload = {
                "event_type": "EXECUTION_INTENT_TRANSITIONED",
                "client_order_id": client,
                "from_state": expected_state,
                "state": new_state,
                "request": json.loads(row["request_json"]),
                "result": (
                    result
                    if result is not None
                    else (
                        json.loads(row["result_json"]) if row["result_json"] else None
                    )
                ),
                "trade_id": trade_id if trade_id is not None else row["trade_id"],
                "causality": {
                    "causation_id": row["last_event_id"],
                    "correlation_id": client,
                    "parent_event_id": row["last_event_id"],
                },
            }
            encoded = _canonical_payload(payload)
            await self._append_event(
                conn, aggregate, event_id, expected_revision, encoded
            )
            await conn.execute(
                """UPDATE aitos_oms_execution_intents
                   SET state=$3,result=COALESCE($4::jsonb,result),
                       trade_id=COALESCE($5,trade_id),revision=$6,
                       last_event_id=$7,updated_at=now()
                   WHERE account_scope=$1 AND client_id=$2""",
                self.scope,
                client,
                new_state,
                (
                    json.dumps(
                        result, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    if result is not None
                    else None
                ),
                trade_id,
                next_revision,
                event_id,
            )
            return {
                "account_scope": self.scope,
                "client_id": client,
                "symbol": row["symbol"],
                "state": new_state,
                "request": json.loads(row["request_json"]),
                "result": payload["result"],
                "trade_id": payload["trade_id"],
                "revision": next_revision,
                "last_event_id": event_id,
            }

    async def transition_with_trade_snapshot(
        self,
        client_id: str,
        *,
        expected_revision: int,
        expected_state: str,
        trade_id: str,
        expected_trade_revision: int,
        trade_snapshot: dict[str, Any],
        ledger_entries: list[dict[str, Any]] | None = None,
        financial_snapshot: dict[str, Any] | None = None,
        expected_financial_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically apply an execution intent, trade snapshot and ledger journal.

        This is the canonical finalization boundary for an execution that has
        already been confirmed by the venue. It commits the intent transition,
        trade aggregate revision, financial ledger lines and both projection
        outbox records in one PostgreSQL transaction. It does not submit orders.
        """
        client = _identifier(client_id, "client_id")
        trade = _identifier(trade_id, "trade_id")
        if len(client) > 180:
            raise ValueError("client_order_id exceeds canonical intent limit")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("positive expected_revision is required")
        if type(expected_trade_revision) is not int or expected_trade_revision < 0:
            raise ValueError("non-negative expected_trade_revision is required")
        new_state = "APPLIED"
        if new_state not in _ALLOWED_TRANSITIONS.get(expected_state, set()):
            raise ValueError(
                f"invalid execution-intent transition {expected_state}->{new_state}"
            )
        if not isinstance(trade_snapshot, dict) or not trade_snapshot:
            raise ValueError("non-empty canonical trade snapshot is required")
        if trade_snapshot.get("trade_id") != trade:
            raise ValueError("trade snapshot identity does not match execution intent")
        snapshot_scope = trade_snapshot.get("account_scope")
        if snapshot_scope != self.scope:
            raise ValueError("trade snapshot account scope does not match writer fence")
        normalized_entries = self.oms._validate_ledger_entries(
            [] if ledger_entries is None else ledger_entries
        )
        if expected_state == "CONFIRMED" and not normalized_entries:
            raise ValueError(
                "confirmed execution requires balanced financial ledger entries"
            )
        for entry in normalized_entries:
            reference = entry.get("reference")
            if reference is not None and len(reference) > 256:
                raise ValueError("financial ledger reference exceeds 256 characters")
        if financial_snapshot is not None and not isinstance(financial_snapshot, dict):
            raise TypeError("financial_snapshot must be an object")
        if (financial_snapshot is None) != (expected_financial_revision is None):
            raise ValueError(
                "financial snapshot and expected financial revision must be "
                "supplied together"
            )
        if expected_financial_revision is not None and (
            type(expected_financial_revision) is not int
            or expected_financial_revision < 0
        ):
            raise ValueError("expected financial revision must be non-negative")

        intent_aggregate = f"intent-{client}"
        intent_event_id = f"intent:{client}:revision:{expected_revision + 1}"
        trade_event_id = f"trade:{trade}:revision:{expected_trade_revision + 1}"
        if len(intent_event_id) > 192 or len(trade_event_id) > 192:
            raise ValueError(
                "canonical execution event identity exceeds 192 characters"
            )
        await self._assert_fence()
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            row = await conn.fetchrow(
                """SELECT symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2 FOR UPDATE""",
                self.scope,
                client,
            )
            if row is None:
                raise OMSConflict("execution intent does not exist")
            if (
                int(row["revision"]) != expected_revision
                or row["state"] != expected_state
            ):
                raise OMSConflict("execution intent revision/state CAS failed")
            if expected_state not in {"CONFIRMED", "PROTECTED"}:
                raise ValueError(
                    "only confirmed execution or protection can be applied"
                )
            if not row["result_json"]:
                raise OMSConflict("execution intent has no durable venue result")
            if row["trade_id"] not in (None, trade):
                raise OMSConflict("execution intent is already bound to another trade")
            request_payload = json.loads(row["request_json"])
            result_payload = json.loads(row["result_json"])
            intent_payload = {
                "event_type": "EXECUTION_INTENT_TRANSITIONED",
                "client_order_id": client,
                "from_state": expected_state,
                "state": "APPLIED",
                "request": request_payload,
                "result": result_payload,
                "trade_id": trade,
                "causality": {
                    "causation_id": row["last_event_id"],
                    "correlation_id": client,
                    "parent_event_id": row["last_event_id"],
                },
            }
            intent_encoded = _canonical_payload(intent_payload)
            await self._append_event(
                conn,
                intent_aggregate,
                intent_event_id,
                expected_revision,
                intent_encoded,
            )

            previous_trade_event = await conn.fetchrow(
                """SELECT event_id FROM aitos_oms_events
                   WHERE account_scope=$1 AND aggregate_id=$2
                   ORDER BY revision DESC LIMIT 1""",
                self.scope,
                trade,
            )
            trade_payload = {
                "event_type": "TRADE_SNAPSHOT",
                "trade_id": trade,
                "account_scope": self.scope,
                "trade": trade_snapshot,
                "causality": {
                    "causation_id": intent_event_id,
                    "correlation_id": client,
                    "parent_event_id": (
                        previous_trade_event["event_id"]
                        if previous_trade_event is not None
                        else None
                    ),
                },
            }
            trade_encoded = _canonical_payload(trade_payload)
            await self._append_event(
                conn,
                trade,
                trade_event_id,
                expected_trade_revision,
                trade_encoded,
            )
            for line_no, entry in enumerate(normalized_entries):
                await conn.execute(
                    """INSERT INTO aitos_oms_financial_ledger
                       (account_scope,event_id,line_no,trade_id,account_code,
                        currency,debit,credit,reference)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    self.scope,
                    trade_event_id,
                    line_no,
                    trade,
                    entry["account_code"],
                    entry["currency"],
                    entry["debit"],
                    entry["credit"],
                    entry["reference"],
                )
            financial_state = None
            financial_event_id = None
            financial_revision = None
            if financial_snapshot is not None:
                currency = _identifier(
                    str(financial_snapshot.get("currency") or ""), "currency"
                )
                financial_aggregate = f"financial:{currency}"
                financial_revision = expected_financial_revision + 1
                financial_event_id = (
                    f"financial:{currency}:revision:{financial_revision}"
                )
                if len(financial_event_id) > 192:
                    raise ValueError("financial event identity exceeds 192 characters")
                previous_financial_event = await conn.fetchrow(
                    """SELECT revision,event_id,payload::text AS payload_json
                       FROM aitos_oms_events
                       WHERE account_scope=$1 AND aggregate_id=$2
                       ORDER BY revision DESC LIMIT 1""",
                    self.scope,
                    financial_aggregate,
                )
                actual_financial_revision = (
                    int(previous_financial_event["revision"])
                    if previous_financial_event is not None
                    else 0
                )
                if actual_financial_revision != expected_financial_revision:
                    raise OMSConflict(
                        "financial aggregate revision CAS failed: "
                        f"expected {expected_financial_revision}, "
                        f"actual {actual_financial_revision}"
                    )
                previous_financial_state = None
                previous_financial_event_id = None
                if previous_financial_event is not None:
                    previous_payload = json.loads(
                        previous_financial_event["payload_json"]
                    )
                    if previous_payload.get("event_type") != "FINANCIAL_STATE_SNAPSHOT":
                        raise OMSConflict(
                            "financial aggregate contains a non-snapshot event"
                        )
                    previous_financial_state = previous_payload.get("financial_state")
                    if not isinstance(previous_financial_state, dict):
                        raise OMSConflict("canonical financial snapshot is malformed")
                    previous_financial_event_id = previous_financial_event["event_id"]
                financial_state = _financial_state_snapshot(
                    financial_snapshot, previous_financial_state
                )
                financial_payload = {
                    "event_type": "FINANCIAL_STATE_SNAPSHOT",
                    "account_scope": self.scope,
                    "financial_state": financial_state,
                    "causality": {
                        "causation_id": trade_event_id,
                        "correlation_id": client,
                        "parent_event_id": previous_financial_event_id,
                    },
                }
                await self._append_event(
                    conn,
                    financial_aggregate,
                    financial_event_id,
                    expected_financial_revision,
                    _canonical_payload(financial_payload),
                )
            next_revision = expected_revision + 1
            await conn.execute(
                """UPDATE aitos_oms_execution_intents
                   SET state='APPLIED',trade_id=$3,revision=$4,last_event_id=$5,
                       updated_at=now()
                   WHERE account_scope=$1 AND client_id=$2""",
                self.scope,
                client,
                trade,
                next_revision,
                intent_event_id,
            )
        return {
            "account_scope": self.scope,
            "client_id": client,
            "symbol": row["symbol"],
            "state": "APPLIED",
            "request": request_payload,
            "result": result_payload,
            "trade_id": trade,
            "revision": next_revision,
            "last_event_id": intent_event_id,
            "trade_event_id": trade_event_id,
            "trade_revision": expected_trade_revision + 1,
            "financial_event_id": financial_event_id,
            "financial_revision": financial_revision,
            "financial_state": financial_state,
        }

    async def append_confirmed_trade_snapshot(
        self,
        client_id: str,
        *,
        expected_revision: int,
        trade_id: str,
        expected_trade_revision: int,
        trade_snapshot: dict[str, Any],
        ledger_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist a confirmed fill and ledger while keeping its intent unresolved.

        Entry intents remain CONFIRMED until the trade's exchange-side
        protection and account-position verification have completed. The
        snapshot, financial lines and projection outbox still commit atomically.
        """
        client = _identifier(client_id, "client_id")
        trade = _identifier(trade_id, "trade_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("positive expected intent revision is required")
        if type(expected_trade_revision) is not int or expected_trade_revision < 0:
            raise ValueError("non-negative expected Trade revision is required")
        if (
            not isinstance(trade_snapshot, dict)
            or trade_snapshot.get("trade_id") != trade
            or trade_snapshot.get("account_scope") != self.scope
        ):
            raise ValueError("confirmed Trade snapshot identity/scope mismatch")
        normalized_entries = self.oms._validate_ledger_entries(ledger_entries)
        if not normalized_entries:
            raise ValueError("confirmed live entry requires balanced ledger entries")
        event_id = f"trade:{trade}:revision:{expected_trade_revision + 1}"
        await self._assert_fence()
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            row = await conn.fetchrow(
                """SELECT symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2 FOR UPDATE""",
                self.scope,
                client,
            )
            if row is None:
                raise OMSConflict("confirmed execution intent does not exist")
            if (
                int(row["revision"]) != expected_revision
                or row["state"] != "CONFIRMED"
            ):
                raise OMSConflict("confirmed execution intent revision/state CAS failed")
            if row["trade_id"] not in {None, trade}:
                raise OMSConflict("confirmed execution intent belongs to another Trade")
            request_payload = json.loads(row["request_json"])
            result_payload = (
                json.loads(row["result_json"]) if row["result_json"] else None
            )
            if (
                request_payload.get("owner_trade_id") != trade
                or request_payload.get("symbol") != row["symbol"]
                or trade_snapshot.get("symbol") != row["symbol"]
                or not isinstance(result_payload, dict)
                or result_payload.get("success") is not True
            ):
                raise OMSConflict("confirmed execution evidence does not match Trade")
            try:
                filled_quantity = Decimal(str(result_payload["filled_quantity"]))
                fill_price = Decimal(str(result_payload["fill_price"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                raise OMSConflict("confirmed execution fill evidence is malformed") from None
            if (
                not filled_quantity.is_finite()
                or not fill_price.is_finite()
                or filled_quantity <= 0
                or fill_price <= 0
            ):
                raise OMSConflict("confirmed execution requires a positive fill")
            previous_trade = await conn.fetchrow(
                """SELECT event_id FROM aitos_oms_events
                   WHERE account_scope=$1 AND aggregate_id=$2
                   ORDER BY revision DESC LIMIT 1""",
                self.scope,
                trade,
            )
            payload = {
                "event_type": "TRADE_SNAPSHOT",
                "trade_id": trade,
                "account_scope": self.scope,
                "trade": trade_snapshot,
                "confirmed_execution": {
                    "client_order_id": client,
                    "intent_revision": expected_revision,
                    "intent_event_id": row["last_event_id"],
                },
                "causality": {
                    "causation_id": row["last_event_id"],
                    "correlation_id": client,
                    "parent_event_id": (
                        previous_trade["event_id"] if previous_trade else None
                    ),
                },
            }
            await self._append_event(
                conn,
                trade,
                event_id,
                expected_trade_revision,
                _canonical_payload(payload),
            )
            for line_no, entry in enumerate(normalized_entries):
                await conn.execute(
                    """INSERT INTO aitos_oms_financial_ledger
                       (account_scope,event_id,line_no,trade_id,account_code,
                        currency,debit,credit,reference)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    self.scope,
                    event_id,
                    line_no,
                    trade,
                    entry["account_code"],
                    entry["currency"],
                    entry["debit"],
                    entry["credit"],
                    entry["reference"],
                )
        return {
            "account_scope": self.scope,
            "client_id": client,
            "trade_id": trade,
            "intent_revision": expected_revision,
            "trade_revision": expected_trade_revision + 1,
            "trade_event_id": event_id,
            "intent_state": "CONFIRMED",
        }

    async def append_protection_child_fill(
        self,
        client_id: str,
        *,
        expected_revision: int,
        expected_state: str,
        trade_id: str,
        expected_trade_revision: int,
        trade_snapshot: dict[str, Any],
        child_fill: dict[str, Any],
        ledger_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Append one cumulative child-fill delta without terminating its parent algo.

        A protective algo may trigger multiple child executions. Each fill gets
        its own intent revision, Trade revision, balanced ledger journal and
        projection outbox in the same PostgreSQL transaction, while the
        protective intent remains APPLIED until the venue reports terminal.
        """
        client = _identifier(client_id, "client_id")
        trade = _identifier(trade_id, "trade_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("positive expected intent revision is required")
        if type(expected_trade_revision) is not int or expected_trade_revision < 0:
            raise ValueError("non-negative expected Trade revision is required")
        if expected_state not in {"APPLIED", "PROTECTED"}:
            raise ValueError(
                "protective child fill requires an accepted protection intent"
            )
        if (
            not isinstance(trade_snapshot, dict)
            or trade_snapshot.get("trade_id") != trade
            or trade_snapshot.get("account_scope") != self.scope
        ):
            raise ValueError("child-fill Trade snapshot identity/scope mismatch")
        if not isinstance(child_fill, dict):
            raise TypeError("child_fill evidence must be an object")
        required_child = {
            "algo_id",
            "child_order_id",
            "cumulative_quantity",
            "cumulative_quote",
            "delta_quantity",
            "delta_quote",
            "status",
        }
        if not required_child.issubset(child_fill):
            raise ValueError("child-fill evidence is incomplete")
        algo_id = _identifier(str(child_fill["algo_id"]), "algo_id")
        child_order_id = _identifier(
            str(child_fill["child_order_id"]), "child_order_id"
        )
        normalized_entries = self.oms._validate_ledger_entries(ledger_entries)
        if not normalized_entries:
            raise ValueError(
                "positive protective child fill requires balanced ledger entries"
            )
        intent_event_id = f"intent:{client}:revision:{expected_revision + 1}"
        trade_event_id = f"trade:{trade}:revision:{expected_trade_revision + 1}"
        await self._assert_fence()
        async with self.oms._pool.acquire() as conn, conn.transaction():
            await self._lock_fence(conn)
            row = await conn.fetchrow(
                """SELECT symbol,state,request::text AS request_json,
                          result::text AS result_json,trade_id,revision,last_event_id
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2 FOR UPDATE""",
                self.scope,
                client,
            )
            if row is None:
                raise OMSConflict("canonical protective intent does not exist")
            if (
                int(row["revision"]) != expected_revision
                or row["state"] != expected_state
            ):
                raise OMSConflict("protective child intent revision/state CAS failed")
            if row["state"] not in {"APPLIED", "PROTECTED"}:
                raise OMSConflict("protective intent is not accepted")
            if row["trade_id"] != trade:
                raise OMSConflict("protective intent is not owned by this Trade")
            request_payload = json.loads(row["request_json"])
            result_payload = (
                json.loads(row["result_json"]) if row["result_json"] else None
            )
            if result_payload is None:
                raise OMSConflict("protective intent has no durable venue acceptance")
            if request_payload.get("owner_trade_id") != trade:
                raise OMSConflict("protective request owner does not match Trade")
            if request_payload.get("symbol") != row["symbol"]:
                raise OMSConflict("protective intent symbol identity is inconsistent")
            accepted_order_id = str(result_payload.get("order_id") or "")
            expected_algo_id = accepted_order_id.removeprefix("algo:")
            if expected_algo_id != algo_id:
                raise OMSConflict("protective child does not belong to accepted algo")
            try:
                original_quantity = Decimal(str(request_payload["quantity"]))
                delta_quantity = Decimal(str(child_fill["delta_quantity"]))
                delta_quote = Decimal(str(child_fill["delta_quote"]))
                cumulative_quantity = Decimal(str(child_fill["cumulative_quantity"]))
                cumulative_quote = Decimal(str(child_fill["cumulative_quote"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                raise ValueError(
                    "protective child fill quantities are invalid"
                ) from None
            child_status = str(child_fill["status"]).upper()
            allowed_child_statuses = {
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
                "REJECTED",
                "PARTIALLY_FILLED_TERMINAL",
            }
            if (
                not all(
                    value.is_finite()
                    for value in (
                        original_quantity,
                        delta_quantity,
                        delta_quote,
                        cumulative_quantity,
                        cumulative_quote,
                    )
                )
                or original_quantity <= 0
                or delta_quantity <= 0
                or delta_quote <= 0
                or cumulative_quantity < delta_quantity
                or cumulative_quote < delta_quote
                or cumulative_quantity > original_quantity
                or child_status not in allowed_child_statuses
                or (
                    child_status == "FILLED"
                    and cumulative_quantity != original_quantity
                )
                or (
                    child_status != "FILLED"
                    and cumulative_quantity >= original_quantity
                )
            ):
                raise OMSConflict(
                    "protective child fill exceeds or violates intent bounds"
                )
            if len(intent_event_id) > 192 or len(trade_event_id) > 192:
                raise ValueError("protective child event identity exceeds limit")
            intent_payload = {
                "event_type": "PROTECTION_CHILD_FILL_APPLIED",
                "client_order_id": client,
                "state": expected_state,
                "trade_id": trade,
                "algo_id": algo_id,
                "child_order_id": child_order_id,
                "child_fill": child_fill,
                "causality": {
                    "causation_id": row["last_event_id"],
                    "correlation_id": client,
                    "parent_event_id": row["last_event_id"],
                },
            }
            await self._append_event(
                conn,
                f"intent-{client}",
                intent_event_id,
                expected_revision,
                _canonical_payload(intent_payload),
            )
            previous_trade = await conn.fetchrow(
                """SELECT event_id FROM aitos_oms_events
                   WHERE account_scope=$1 AND aggregate_id=$2
                   ORDER BY revision DESC LIMIT 1""",
                self.scope,
                trade,
            )
            trade_payload = {
                "event_type": "TRADE_SNAPSHOT",
                "trade_id": trade,
                "account_scope": self.scope,
                "trade": trade_snapshot,
                "protection_child_fill": child_fill,
                "causality": {
                    "causation_id": intent_event_id,
                    "correlation_id": client,
                    "parent_event_id": (
                        previous_trade["event_id"] if previous_trade else None
                    ),
                },
            }
            await self._append_event(
                conn,
                trade,
                trade_event_id,
                expected_trade_revision,
                _canonical_payload(trade_payload),
            )
            for line_no, entry in enumerate(normalized_entries):
                await conn.execute(
                    """INSERT INTO aitos_oms_financial_ledger
                       (account_scope,event_id,line_no,trade_id,account_code,
                        currency,debit,credit,reference)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    self.scope,
                    trade_event_id,
                    line_no,
                    trade,
                    entry["account_code"],
                    entry["currency"],
                    entry["debit"],
                    entry["credit"],
                    entry["reference"],
                )
            cumulative_quantity = Decimal(str(child_fill["cumulative_quantity"]))
            cumulative_quote = Decimal(str(child_fill["cumulative_quote"]))
            child_status = str(child_fill["status"]).upper()
            if (
                not cumulative_quantity.is_finite()
                or not cumulative_quote.is_finite()
                or cumulative_quantity <= 0
                or cumulative_quote <= 0
                or cumulative_quantity.as_tuple().exponent < -18
                or cumulative_quote.as_tuple().exponent < -18
                or not child_status
            ):
                raise ValueError("protective child cumulative watermark is invalid")
            watermark = await conn.fetchrow(
                """SELECT trade_id,symbol,cumulative_quantity,cumulative_quote,status
                   FROM aitos_oms_protection_child_fills
                   WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3
                   FOR UPDATE""",
                self.scope,
                algo_id,
                child_order_id,
            )
            if watermark is not None:
                if (
                    watermark["trade_id"] != trade
                    or watermark["symbol"] != row["symbol"]
                ):
                    raise OMSConflict(
                        "protective child watermark is owned by another Trade"
                    )
                previous_quantity = Decimal(str(watermark["cumulative_quantity"]))
                previous_quote = Decimal(str(watermark["cumulative_quote"]))
                if (
                    cumulative_quantity != previous_quantity + delta_quantity
                    or cumulative_quote != previous_quote + delta_quote
                ):
                    raise OMSConflict(
                        "protective child cumulative fill delta is inconsistent"
                    )
                await conn.execute(
                    """UPDATE aitos_oms_protection_child_fills
                       SET cumulative_quantity=$4,cumulative_quote=$5,status=$6,
                           updated_at=now()
                       WHERE account_scope=$1 AND algo_id=$2 AND child_order_id=$3""",
                    self.scope,
                    algo_id,
                    child_order_id,
                    cumulative_quantity,
                    cumulative_quote,
                    child_status,
                )
            else:
                if (
                    cumulative_quantity != delta_quantity
                    or cumulative_quote != delta_quote
                ):
                    raise OMSConflict(
                        "initial protective child cumulative fill lacks a baseline"
                    )
                await conn.execute(
                    """INSERT INTO aitos_oms_protection_child_fills
                       (account_scope,algo_id,child_order_id,trade_id,symbol,
                        cumulative_quantity,cumulative_quote,status)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                    self.scope,
                    algo_id,
                    child_order_id,
                    trade,
                    row["symbol"],
                    cumulative_quantity,
                    cumulative_quote,
                    child_status,
                )
            await self._append_protection_event(
                conn,
                f"protection-child-{algo_id}-{child_order_id}",
                f"protection-child:{algo_id}:{child_order_id}",
                {
                    "event_type": "PROTECTION_CHILD_FILL_WATERMARK_UPDATED",
                    "account_scope": self.scope,
                    "algo_id": algo_id,
                    "child_order_id": child_order_id,
                    "trade_id": trade,
                    "symbol": row["symbol"],
                    "cumulative_quantity": format(cumulative_quantity, "f"),
                    "cumulative_quote": format(cumulative_quote, "f"),
                    "status": child_status,
                },
            )
            next_revision = expected_revision + 1
            await conn.execute(
                """UPDATE aitos_oms_execution_intents
                   SET trade_id=$3,revision=$4,last_event_id=$5,updated_at=now()
                   WHERE account_scope=$1 AND client_id=$2""",
                self.scope,
                client,
                trade,
                next_revision,
                intent_event_id,
            )
        return {
            "account_scope": self.scope,
            "client_id": client,
            "symbol": row["symbol"],
            "state": expected_state,
            "request": request_payload,
            "result": result_payload,
            "trade_id": trade,
            "revision": next_revision,
            "last_event_id": intent_event_id,
            "trade_event_id": trade_event_id,
            "trade_revision": expected_trade_revision + 1,
            "child_fill": child_fill,
        }

    async def record_result(self, client_id: str, result: Any) -> dict[str, Any]:
        """Persist authoritative terminal venue evidence for a submitted intent."""
        if not isinstance(result, dict):
            raise TypeError("execution result payload must be an object")
        row = await self.get(client_id)
        if row is None:
            raise OMSConflict("execution intent does not exist")
        if row["state"] != "SUBMITTING":
            raise OMSConflict(
                "only a submitting intent can accept first venue evidence"
            )
        filled = Decimal(str(result.get("filled_quantity", "0")))
        price = Decimal(str(result.get("fill_price", "0")))
        success = result.get("success")
        if type(success) is not bool or not filled.is_finite() or not price.is_finite():
            raise ValueError("execution result evidence is malformed")
        if filled > 0 and price > 0:
            next_state = "CONFIRMED"
        elif filled == 0 and price == 0 and not success:
            next_state = "REJECTED"
        else:
            raise ValueError("execution result fill economics are inconsistent")
        return await self.transition(
            client_id,
            expected_revision=int(row["revision"]),
            expected_state="SUBMITTING",
            new_state=next_state,
            result=result,
        )

    async def record_protection(self, client_id: str, result: Any) -> dict[str, Any]:
        """Persist a venue-accepted, currently unfilled protective order."""
        if not isinstance(result, dict):
            if all(
                hasattr(result, name)
                for name in (
                    "order_id",
                    "symbol",
                    "side",
                    "filled_quantity",
                    "fill_price",
                )
            ):
                result = {
                    "order_id": str(result.order_id),
                    "symbol": str(result.symbol),
                    "side": str(getattr(result.side, "value", result.side)),
                    "filled_quantity": format(
                        Decimal(str(result.filled_quantity)), "f"
                    ),
                    "fill_price": format(Decimal(str(result.fill_price)), "f"),
                    "success": bool(getattr(result, "success", True)),
                    "client_order_id": getattr(result, "client_order_id", client_id),
                }
            else:
                raise TypeError(
                    "protective result payload must be an object or OrderResult"
                )
        row = await self.get(client_id)
        if row is None:
            raise OMSConflict("protective execution intent does not exist")
        if row["state"] != "SUBMITTING":
            raise OMSConflict("only a submitting protection intent can be accepted")
        try:
            filled = Decimal(str(result.get("filled_quantity", "0")))
            trigger = Decimal(str(result.get("fill_price", "0")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("protective acceptance evidence is malformed") from None
        if (
            filled != 0
            or not filled.is_finite()
            or not trigger.is_finite()
            or trigger <= 0
        ):
            raise ValueError(
                "protective acceptance must be unfilled with a positive trigger"
            )
        order_id = result.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("protective acceptance requires a durable venue order ID")
        return await self.transition(
            client_id,
            expected_revision=int(row["revision"]),
            expected_state="SUBMITTING",
            new_state="PROTECTED",
            result=result,
        )

    async def mark_terminal(
        self, client_id: str, *, terminal_state: str = "TERMINAL"
    ) -> dict[str, Any]:
        """Mark a previously accepted protection/intent terminal without losing evidence."""
        if terminal_state != "TERMINAL":
            raise ValueError("terminal_state must be TERMINAL")
        row = await self.get(client_id)
        if row is None:
            raise OMSConflict("execution intent does not exist")
        if row["state"] == "TERMINAL":
            return row
        if row["state"] not in {"PROTECTED", "APPLIED"}:
            raise OMSConflict("only accepted or applied intents can become terminal")
        return await self.transition(
            client_id,
            expected_revision=int(row["revision"]),
            expected_state=str(row["state"]),
            new_state="TERMINAL",
        )

    async def append_financial_snapshot(
        self, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Append a scope-fenced, revisioned account valuation and drawdown snapshot."""
        if not isinstance(snapshot, dict):
            raise TypeError("financial snapshot must be an object")
        currency = _identifier(str(snapshot.get("currency") or ""), "currency")
        if not 2 <= len(currency) <= 12 or not all(
            "A" <= char <= "Z" or "0" <= char <= "9" for char in currency
        ):
            raise ValueError("financial currency must be an uppercase code")
        aggregate = f"financial:{currency}"
        await self._assert_fence()
        events = await self.oms.read_events(self.scope, aggregate)
        expected_revision = events[-1].revision if events else 0
        previous_state = None
        previous_event_id = None
        if events:
            prior_payload = events[-1].payload
            if prior_payload.get("event_type") != "FINANCIAL_STATE_SNAPSHOT":
                raise OMSConflict("financial aggregate contains a non-snapshot event")
            previous_state = prior_payload.get("financial_state")
            if not isinstance(previous_state, dict):
                raise OMSConflict("canonical financial snapshot is malformed")
            previous_event_id = events[-1].event_id
        financial_state = _financial_state_snapshot(snapshot, previous_state)
        revision = expected_revision + 1
        event_id = f"financial:{currency}:revision:{revision}"
        payload = {
            "event_type": "FINANCIAL_STATE_SNAPSHOT",
            "account_scope": self.scope,
            "financial_state": financial_state,
            "causality": {
                "causation_id": f"runtime-valuation:{currency}:{revision}",
                "correlation_id": self.scope,
                "parent_event_id": previous_event_id,
            },
        }
        event = await self.oms.append(
            self.fence,
            aggregate_id=aggregate,
            event_id=event_id,
            expected_revision=expected_revision,
            payload=payload,
        )
        if event.revision != revision:
            raise OMSConflict("financial snapshot committed at an unexpected revision")
        return financial_state

    async def latest_financial_state(self, currency: str) -> dict[str, Any] | None:
        """Read the most recent canonical valuation snapshot for this currency."""
        normalized = _identifier(currency, "currency")
        if not 2 <= len(normalized) <= 12 or not all(
            "A" <= char <= "Z" or "0" <= char <= "9" for char in normalized
        ):
            raise ValueError("financial currency must be an uppercase code")
        events = await self.oms.read_events(self.scope, f"financial:{normalized}")
        for event in reversed(events):
            if event.payload.get("event_type") != "FINANCIAL_STATE_SNAPSHOT":
                raise OMSConflict("financial aggregate contains a non-snapshot event")
            state = event.payload.get("financial_state")
            if not isinstance(state, dict) or state.get("currency") != normalized:
                raise OMSConflict("canonical financial snapshot is malformed")
            return state
        return None

    @staticmethod
    def _decode_intent_row(row: Any) -> dict[str, Any]:
        payload = dict(row)
        for field in ("request", "result"):
            value = payload.get(field)
            if isinstance(value, str):
                try:
                    payload[field] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise OMSIntegrityError(
                        f"canonical execution intent {field} JSON is malformed"
                    ) from exc
        if not isinstance(payload.get("request"), dict):
            raise OMSIntegrityError("canonical execution intent request is malformed")
        if payload.get("result") is not None and not isinstance(
            payload.get("result"), dict
        ):
            raise OMSIntegrityError("canonical execution intent result is malformed")
        return payload

    async def get(self, client_id: str) -> dict[str, Any] | None:
        client = _identifier(client_id, "client_id")
        async with self.oms._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT client_id,symbol,state,request,result,trade_id,revision,
                          last_event_id,updated_at
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1 AND client_id=$2""",
                self.scope,
                client,
            )
        return self._decode_intent_row(row) if row else None

    async def unresolved(self, symbol: str | None = None) -> list[dict[str, Any]]:
        async with self.oms._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT client_id,symbol,state,request,result,trade_id,revision,
                          last_event_id,updated_at
                   FROM aitos_oms_execution_intents
                   WHERE account_scope=$1
                     AND state NOT IN ('APPLIED','REJECTED','TERMINAL')
                     AND ($2::text IS NULL OR symbol=$2)
                   ORDER BY client_id""",
                self.scope,
                symbol,
            )
        return [self._decode_intent_row(row) for row in rows]

    async def assert_ready(self) -> None:
        rows = await self.unresolved()
        if rows:
            row = rows[0]
            raise OMSConflict(
                "startup requires execution-intent reconciliation for "
                f"{row['symbol']}/{row['client_id']}"
            )

    async def _assert_fence(self) -> None:
        async with self.oms._pool.acquire() as conn:
            await self._check_fence(conn)

    async def _check_fence(self, conn: Any) -> None:
        owner = await conn.fetchrow(
            """SELECT writer_id,generation FROM aitos_oms_writer_fences
               WHERE account_scope=$1""",
            self.scope,
        )
        if owner is None or (owner["writer_id"], int(owner["generation"])) != (
            self.fence.writer_id,
            self.fence.generation,
        ):
            raise OMSConflict("stale writer fencing token")

    async def _lock_fence(self, conn: Any) -> None:
        owner = await conn.fetchrow(
            """SELECT writer_id,generation FROM aitos_oms_writer_fences
               WHERE account_scope=$1 FOR UPDATE""",
            self.scope,
        )
        if owner is None or (owner["writer_id"], int(owner["generation"])) != (
            self.fence.writer_id,
            self.fence.generation,
        ):
            raise OMSConflict("stale writer fencing token")

    async def _append_event(
        self,
        conn: Any,
        aggregate: str,
        event_id: str,
        expected_revision: int,
        encoded: str,
    ) -> None:
        await conn.execute(
            """INSERT INTO aitos_oms_aggregate_heads
               (account_scope,aggregate_id,revision,event_hash)
               VALUES($1,$2,0,$3) ON CONFLICT DO NOTHING""",
            self.scope,
            aggregate,
            "0" * 64,
        )
        head = await conn.fetchrow(
            """SELECT revision,event_hash FROM aitos_oms_aggregate_heads
               WHERE account_scope=$1 AND aggregate_id=$2 FOR UPDATE""",
            self.scope,
            aggregate,
        )
        actual = int(head["revision"])
        if actual != expected_revision:
            raise OMSConflict(
                "execution intent revision CAS failed: "
                f"expected {expected_revision}, actual {actual}"
            )
        revision = actual + 1
        previous_hash = head["event_hash"]
        digest = _event_hash(
            self.scope,
            aggregate,
            revision,
            event_id,
            self.fence.writer_id,
            self.fence.generation,
            previous_hash,
            encoded,
        )
        await conn.execute(
            """INSERT INTO aitos_oms_events
               (account_scope,aggregate_id,revision,event_id,writer_id,generation,
                payload,previous_hash,event_hash)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)""",
            self.scope,
            aggregate,
            revision,
            event_id,
            self.fence.writer_id,
            self.fence.generation,
            encoded,
            previous_hash,
            digest,
        )
        await conn.execute(
            """UPDATE aitos_oms_aggregate_heads SET revision=$3,event_hash=$4
               WHERE account_scope=$1 AND aggregate_id=$2""",
            self.scope,
            aggregate,
            revision,
            digest,
        )
        await conn.execute(
            """INSERT INTO aitos_oms_outbox(account_scope,aggregate_id,revision)
               VALUES($1,$2,$3)""",
            self.scope,
            aggregate,
            revision,
        )
