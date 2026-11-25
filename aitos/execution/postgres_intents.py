"""PostgreSQL-backed execution intents sharing the canonical OMS transaction.

This adapter is async and deliberately separate from the legacy synchronous
SQLite journal. Runtime adoption must be explicit; it never silently dual-writes.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from aitos.execution.postgres_oms import (
    OMSConflict,
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
    if not isinstance(snapshot, dict) or set(snapshot) != required:
        raise ValueError(
            "financial snapshot must contain currency, starting_equity, "
            "realized_pnl, unrealized_pnl, fees and funding only"
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
        equity = starting + realized + unrealized + funding - fees
        if previous is not None:
            if previous.get("currency") != currency:
                raise ValueError("financial currency cannot change within an aggregate")
            previous_starting = Decimal(str(previous["starting_equity"]))
            if previous_starting != starting:
                raise ValueError("starting equity is immutable within a currency ledger")
            previous_peak = Decimal(str(previous["peak_equity"]))
            previous_max_drawdown = Decimal(str(previous["max_drawdown"]))
            previous_max_drawdown_pct = Decimal(
                str(previous["max_drawdown_pct"])
            )
        else:
            previous_peak = starting
            previous_max_drawdown = Decimal(0)
            previous_max_drawdown_pct = Decimal(0)
        peak = max(previous_peak, equity)
        drawdown = max(Decimal(0), peak - equity)
        drawdown_pct = (
            drawdown / peak * Decimal(100) if peak > 0 else Decimal(0)
        )
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
                "financial snapshot and expected financial revision must be supplied together"
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
                    previous_financial_state = previous_payload.get(
                        "financial_state"
                    )
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

    async def latest_financial_state(
        self, currency: str
    ) -> dict[str, Any] | None:
        """Read the most recent canonical valuation snapshot for this currency."""
        normalized = _identifier(currency, "currency")
        if not 2 <= len(normalized) <= 12 or not all(
            "A" <= char <= "Z" or "0" <= char <= "9" for char in normalized
        ):
            raise ValueError("financial currency must be an uppercase code")
        events = await self.oms.read_events(
            self.scope, f"financial:{normalized}"
        )
        for event in reversed(events):
            if event.payload.get("event_type") != "FINANCIAL_STATE_SNAPSHOT":
                raise OMSConflict("financial aggregate contains a non-snapshot event")
            state = event.payload.get("financial_state")
            if not isinstance(state, dict) or state.get("currency") != normalized:
                raise OMSConflict("canonical financial snapshot is malformed")
            return state
        return None

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
        return dict(row) if row else None

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
        return [dict(row) for row in rows]

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
                f"execution intent revision CAS failed: expected {expected_revision}, actual {actual}"
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
