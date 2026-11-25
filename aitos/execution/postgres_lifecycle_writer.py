"""Canonical PostgreSQL journal adapter for TradeLifecycle state writes.

PostgreSQL commits the authoritative aggregate revision before the legacy
execution-state callback updates SQLite/ClickHouse projections. A projection
failure therefore cannot roll back or supersede canonical OMS history.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aitos.execution.postgres_oms import PostgresCanonicalOMS, WriterFence


class PostgresLifecycleStateWriter:
    def __init__(
        self,
        oms: PostgresCanonicalOMS,
        fence: WriterFence,
        projection_writer: Callable[[Any], Awaitable[None]],
    ) -> None:
        if not isinstance(oms, PostgresCanonicalOMS):
            raise TypeError("PostgresCanonicalOMS is required")
        if not isinstance(fence, WriterFence):
            raise TypeError("server-issued PostgreSQL writer fence is required")
        if fence.account_scope.strip() == "" or not callable(projection_writer):
            raise ValueError("scope and durable projection writer are required")
        self.oms = oms
        self.fence = fence
        self.projection_writer = projection_writer
        self._revisions: dict[str, int] = {}
        self._last_event_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _revision(self, trade_id: str) -> int:
        if trade_id not in self._revisions:
            events = await self.oms.read_events(self.fence.account_scope, trade_id)
            self._revisions[trade_id] = events[-1].revision if events else 0
            if events:
                self._last_event_ids[trade_id] = events[-1].event_id
        return self._revisions[trade_id]

    async def __call__(self, trade: Any) -> None:
        trade_id = str(getattr(trade, "trade_id", "")).strip()
        to_dict = getattr(trade, "to_dict", None)
        if not trade_id or not callable(to_dict):
            raise ValueError(
                "TradeLifecycle writer requires a persisted Trade aggregate"
            )
        lock = self._locks.setdefault(trade_id, asyncio.Lock())
        async with lock:
            revision = await self._revision(trade_id)
            next_revision = revision + 1
            event_id = f"trade:{trade_id}:revision:{next_revision}"
            event = await self.oms.append(
                self.fence,
                aggregate_id=trade_id,
                event_id=event_id,
                expected_revision=revision,
                payload={
                    "event_type": "TRADE_SNAPSHOT",
                    "trade_id": trade_id,
                    "trade": to_dict(),
                },
                causation_id=str(
                    getattr(trade, "causation_id", f"lifecycle:{trade_id}")
                ),
                correlation_id=trade_id,
                parent_event_id=self._last_event_ids.get(trade_id),
            )
            if event.revision != next_revision:
                raise RuntimeError(
                    "canonical OMS returned an unexpected aggregate revision"
                )
            self._revisions[trade_id] = next_revision
            self._last_event_ids[trade_id] = event.event_id
            # This is a projection only. If it fails, the committed canonical
            # event remains pending in the transactional outbox for replay.
            await self.projection_writer(trade)
            await self.oms.mark_projected(
                self.fence.account_scope,
                trade_id,
                event.revision,
                event_hash=event.event_hash,
            )

    async def append_cost_adjustment(
        self,
        trade_id: str,
        adjustment_id: str,
        kind: str,
        amount_usd: Any,
    ) -> Any:
        """Append an idempotent fee/funding journal event to its trade aggregate.

        The adjustment is committed to the canonical trade event chain and
        balanced financial ledger under the same writer fence. Lifecycle
        snapshot writes and cost events share the per-trade serialization lock.
        """
        from decimal import Decimal, InvalidOperation

        trade = str(trade_id).strip()
        adjustment = str(adjustment_id).strip()
        if not trade or not adjustment:
            raise ValueError("trade_id and adjustment_id are required")
        if kind not in {"fee", "funding"}:
            raise ValueError("cost adjustment kind must be fee or funding")
        try:
            amount = Decimal(str(amount_usd))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("cost adjustment amount must be decimal") from None
        if not amount.is_finite() or amount == 0:
            raise ValueError("cost adjustment amount must be finite and non-zero")
        if kind == "fee" and amount < 0:
            raise ValueError("fee adjustment must be a positive cost")

        absolute = abs(amount)
        if kind == "fee":
            debit_account, credit_account = "EXPENSE_FEES", "CASH"
        elif amount > 0:
            debit_account, credit_account = "CASH", "FUNDING_INCOME"
        else:
            debit_account, credit_account = "FUNDING_EXPENSE", "CASH"
        event_id = f"income:{adjustment}"
        if len(event_id) > 192:
            raise ValueError("income event identity exceeds canonical OMS limit")

        lock = self._locks.setdefault(trade, asyncio.Lock())
        async with lock:
            events = await self.oms.read_events(self.fence.account_scope, trade)
            existing = next(
                (item for item in events if item.event_id == event_id), None
            )
            if existing is not None:
                expected_ledger = [
                    {
                        "account_code": debit_account,
                        "currency": "USDT",
                        "debit": format(absolute, "f"),
                        "credit": "0",
                        "reference": adjustment,
                    },
                    {
                        "account_code": credit_account,
                        "currency": "USDT",
                        "debit": "0",
                        "credit": format(absolute, "f"),
                        "reference": adjustment,
                    },
                ]
                if (
                    existing.payload.get("event_type") != "TRADE_COST_ADJUSTMENT"
                    or existing.payload.get("trade_id") != trade
                    or existing.payload.get("adjustment_id") != adjustment
                    or existing.payload.get("kind") != kind
                    or existing.payload.get("amount") != format(amount, "f")
                    or existing.payload.get("financial_ledger") != expected_ledger
                ):
                    raise RuntimeError(
                        "canonical income event identity was reused with different content"
                    )
                event = existing
            else:
                revision = events[-1].revision if events else 0
                parent_event_id = events[-1].event_id if events else None
                event = await self.oms.append(
                    self.fence,
                    aggregate_id=trade,
                    event_id=event_id,
                    expected_revision=revision,
                    payload={
                        "event_type": "TRADE_COST_ADJUSTMENT",
                        "trade_id": trade,
                        "adjustment_id": adjustment,
                        "kind": kind,
                        "amount": format(amount, "f"),
                        "currency": "USDT",
                        "causality": {
                            "causation_id": adjustment,
                            "correlation_id": trade,
                            "parent_event_id": parent_event_id,
                        },
                    },
                    ledger_entries=[
                        {
                            "account_code": debit_account,
                            "currency": "USDT",
                            "debit": format(absolute, "f"),
                            "credit": "0",
                            "reference": adjustment,
                        },
                        {
                            "account_code": credit_account,
                            "currency": "USDT",
                            "debit": "0",
                            "credit": format(absolute, "f"),
                            "reference": adjustment,
                        },
                    ],
                )
            if event.revision >= self._revisions.get(trade, 0):
                self._revisions[trade] = event.revision
                self._last_event_ids[trade] = event.event_id
            return event

    async def apply_cost_adjustment(
        self,
        trade: Any,
        adjustment_id: str,
        kind: str,
        amount_usd: Any,
    ) -> Any:
        """Atomically persist a fee/funding adjustment, Trade snapshot and ledger.

        The caller applies the idempotent adjustment to its in-memory Trade
        without publishing or writing a legacy projection. PostgreSQL then
        commits the event, balanced journal lines and projection outbox as one
        transaction. A projection failure is replayable from the outbox.
        """
        from decimal import Decimal, InvalidOperation

        trade_id = str(getattr(trade, "trade_id", "") or "").strip()
        to_dict = getattr(trade, "to_dict", None)
        adjustment = str(adjustment_id).strip()
        if not trade_id or not callable(to_dict) or not adjustment:
            raise ValueError("Trade and stable adjustment identity are required")
        if kind not in {"fee", "funding"}:
            raise ValueError("cost adjustment kind must be fee or funding")
        try:
            amount = Decimal(str(amount_usd))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("cost adjustment amount must be decimal") from None
        if not amount.is_finite() or amount == 0:
            raise ValueError("cost adjustment amount must be finite and non-zero")
        if kind == "fee" and amount < 0:
            raise ValueError("fee adjustment must be a positive cost")
        scope = str(getattr(trade, "account_scope", "") or "")
        if scope and scope != self.fence.account_scope:
            raise ValueError("Trade account scope differs from canonical OMS writer")
        trade.account_scope = self.fence.account_scope
        snapshot = to_dict()
        if not isinstance(snapshot, dict) or snapshot.get("trade_id") != trade_id:
            raise ValueError("cost adjustment Trade snapshot identity is invalid")
        absolute = abs(amount)
        if kind == "fee":
            debit_account, credit_account = "EXPENSE_FEES", "CASH"
        elif amount > 0:
            debit_account, credit_account = "CASH", "FUNDING_INCOME"
        else:
            debit_account, credit_account = "FUNDING_EXPENSE", "CASH"
        event_id = f"income:{adjustment}"
        if len(event_id) > 192:
            raise ValueError("income event identity exceeds canonical OMS limit")
        lock = self._locks.setdefault(trade_id, asyncio.Lock())
        async with lock:
            events = await self.oms.read_events(self.fence.account_scope, trade_id)
            existing = next(
                (item for item in events if item.event_id == event_id), None
            )
            if existing is not None:
                payload = existing.payload
                if (
                    payload.get("event_type") != "TRADE_COST_ADJUSTMENT"
                    or payload.get("adjustment_id") != adjustment
                    or payload.get("kind") != kind
                    or payload.get("amount") != format(amount, "f")
                ):
                    raise RuntimeError("canonical income event identity was reused")
                event = existing
            else:
                revision = events[-1].revision if events else 0
                parent_event_id = events[-1].event_id if events else None
                event = await self.oms.append(
                    self.fence,
                    aggregate_id=trade_id,
                    event_id=event_id,
                    expected_revision=revision,
                    payload={
                        "event_type": "TRADE_COST_ADJUSTMENT",
                        "trade_id": trade_id,
                        "adjustment_id": adjustment,
                        "kind": kind,
                        "amount": format(amount, "f"),
                        "currency": "USDT",
                        "trade": snapshot,
                    },
                    ledger_entries=[
                        {
                            "account_code": debit_account,
                            "currency": "USDT",
                            "debit": format(absolute, "f"),
                            "credit": "0",
                            "reference": adjustment,
                        },
                        {
                            "account_code": credit_account,
                            "currency": "USDT",
                            "debit": "0",
                            "credit": format(absolute, "f"),
                            "reference": adjustment,
                        },
                    ],
                    causation_id=adjustment,
                    correlation_id=trade_id,
                    parent_event_id=parent_event_id,
                )
            self._revisions[trade_id] = event.revision
            self._last_event_ids[trade_id] = event.event_id
            async with self.oms._pool.acquire() as conn:
                outbox_row = await conn.fetchrow(
                    """SELECT projected_at FROM aitos_oms_outbox
                       WHERE account_scope=$1 AND aggregate_id=$2 AND revision=$3""",
                    self.fence.account_scope,
                    trade_id,
                    event.revision,
                )
            if outbox_row is None:
                raise RuntimeError("canonical cost event has no projection outbox row")
            if outbox_row["projected_at"] is None:
                latest = await self.latest_snapshot(trade_id)
                if not isinstance(latest, dict):
                    raise RuntimeError(
                        "canonical cost event has no recoverable Trade snapshot"
                    )
                from aitos.trading.persistent_state import _trade_from_dict

                latest_trade = _trade_from_dict(latest)
                await self.projection_writer(latest_trade)
                await self.oms.mark_projected(
                    self.fence.account_scope,
                    trade_id,
                    event.revision,
                    event_hash=event.event_hash,
                )
            return event

    async def apply_execution_intent(
        self,
        intents: Any,
        client_id: str,
        trade: Any,
        *,
        ledger_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply a confirmed venue intent with its trade snapshot.

        PostgreSQL commits the intent APPLIED transition, causal Trade snapshot,
        balanced financial journal and their outbox records in one transaction.
        The legacy stores are updated only after that canonical commit.
        """
        if getattr(intents, "oms", None) is not self.oms:
            raise ValueError("execution intents must share the lifecycle writer OMS")
        if getattr(intents, "fence", None) != self.fence:
            raise ValueError("execution intents must use the lifecycle writer fence")
        trade_id = str(getattr(trade, "trade_id", "")).strip()
        to_dict = getattr(trade, "to_dict", None)
        if not trade_id or not callable(to_dict):
            raise ValueError("execution finalization requires a persisted Trade")
        current_scope = str(getattr(trade, "account_scope", "") or "")
        if current_scope and current_scope != self.fence.account_scope:
            raise ValueError("trade account scope differs from canonical OMS writer")
        trade.account_scope = self.fence.account_scope
        intent = await intents.get(client_id)
        if intent is None:
            raise RuntimeError("canonical execution intent is missing")
        expected_state = str(intent["state"])
        if expected_state not in {"CONFIRMED", "PROTECTED"}:
            raise RuntimeError(
                f"canonical execution intent cannot be applied from {expected_state}"
            )
        trade_revision = await self._revision(trade_id)
        committed = await intents.transition_with_trade_snapshot(
            client_id,
            expected_revision=int(intent["revision"]),
            expected_state=expected_state,
            trade_id=trade_id,
            expected_trade_revision=trade_revision,
            trade_snapshot=to_dict(),
            ledger_entries=[] if ledger_entries is None else ledger_entries,
        )
        self._revisions[trade_id] = committed["trade_revision"]
        self._last_event_ids[trade_id] = committed["trade_event_id"]
        # Projection is deliberately outside PostgreSQL's commit. Its outbox
        # row remains durable and replayable when the projection callback fails.
        await self.projection_writer(trade)
        trade_events = await self.oms.read_events(self.fence.account_scope, trade_id)
        trade_event = next(
            (
                event
                for event in reversed(trade_events)
                if event.event_id == committed["trade_event_id"]
            ),
            None,
        )
        if trade_event is None:
            raise RuntimeError("canonical Trade event disappeared after commit")
        await self.oms.mark_projected(
            self.fence.account_scope,
            trade_id,
            committed["trade_revision"],
            event_hash=trade_event.event_hash,
        )
        return committed

    async def apply_protection_child_fill(
        self,
        intents: Any,
        client_id: str,
        trade: Any,
        *,
        child_fill: dict[str, Any],
        ledger_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically append one partial/terminal child-fill delta.

        The parent protective intent stays APPLIED while its algo order remains
        active. Each child fill advances both intent and Trade revisions and
        posts a balanced journal in the same PostgreSQL transaction, allowing
        subsequent cumulative fills to be applied without reopening the intent.
        """
        if getattr(intents, "oms", None) is not self.oms:
            raise ValueError("execution intents must share the lifecycle writer OMS")
        if getattr(intents, "fence", None) != self.fence:
            raise ValueError("execution intents must use the lifecycle writer fence")
        trade_id = str(getattr(trade, "trade_id", "")).strip()
        to_dict = getattr(trade, "to_dict", None)
        if not trade_id or not callable(to_dict):
            raise ValueError("protective child fill requires a persisted Trade")
        current_scope = str(getattr(trade, "account_scope", "") or "")
        if current_scope and current_scope != self.fence.account_scope:
            raise ValueError("trade account scope differs from canonical OMS writer")
        trade.account_scope = self.fence.account_scope
        lock = self._locks.setdefault(trade_id, asyncio.Lock())
        async with lock:
            intent = await intents.get(client_id)
            if intent is None:
                raise RuntimeError("canonical protective intent is missing")
            expected_state = str(intent["state"])
            if expected_state not in {"APPLIED", "PROTECTED"}:
                raise RuntimeError(
                    f"protective child fill cannot apply from {expected_state}"
                )
            revision = await self._revision(trade_id)
            committed = await intents.append_protection_child_fill(
                client_id,
                expected_revision=int(intent["revision"]),
                expected_state=expected_state,
                trade_id=trade_id,
                expected_trade_revision=revision,
                trade_snapshot=to_dict(),
                child_fill=child_fill,
                ledger_entries=ledger_entries,
            )
            self._revisions[trade_id] = committed["trade_revision"]
            self._last_event_ids[trade_id] = committed["trade_event_id"]
            await self.projection_writer(trade)
            events = await self.oms.read_events(self.fence.account_scope, trade_id)
            event = next(
                (
                    item
                    for item in reversed(events)
                    if item.event_id == committed["trade_event_id"]
                ),
                None,
            )
            if event is None:
                raise RuntimeError("canonical protective child Trade event disappeared")
            await self.oms.mark_projected(
                self.fence.account_scope,
                trade_id,
                committed["trade_revision"],
                event_hash=event.event_hash,
            )
            return committed

    async def restore_lifecycle(self, lifecycle: Any) -> dict[str, int]:
        """Replace projected lifecycle state with verified canonical OMS snapshots.

        In-flight/nonterminal states other than POSITION_OPENED and
        POSITION_CLOSED are deliberately rejected: they require execution
        intent and venue reconciliation before the lifecycle can safely resume.
        """
        from aitos.models.trade import TradeLifecycleState
        from aitos.trading.persistent_state import _trade_from_dict

        open_trades = {}
        closed_trades = []
        projected_ids = set(getattr(lifecycle, "_open_trades", {}))
        aggregate_ids = await self.oms.list_aggregate_ids(self.fence.account_scope)
        missing_canonical = projected_ids - set(aggregate_ids)
        if missing_canonical:
            raise RuntimeError(
                "canonical OMS is missing projected open trades; controlled import "
                "is required before live execution: "
                + ", ".join(sorted(missing_canonical))
            )
        for trade_id in aggregate_ids:
            events = await self.oms.read_events(self.fence.account_scope, trade_id)
            event_types = {str(event.payload.get("event_type", "")) for event in events}
            if "TRADE_SNAPSHOT" not in event_types:
                # Intent and financial aggregates share the canonical event
                # journal but are not TradeLifecycle positions.
                if event_types and all(
                    event_type.startswith("EXECUTION_INTENT_")
                    or event_type == "FINANCIAL_STATE_SNAPSHOT"
                    for event_type in event_types
                ):
                    continue
                raise RuntimeError(
                    f"canonical OMS aggregate {trade_id} has no recognized trade snapshot"
                )
            snapshot = await self.latest_snapshot(trade_id)
            if snapshot is None:
                raise RuntimeError(
                    f"canonical OMS aggregate {trade_id} has no trade snapshot"
                )
            trade = _trade_from_dict(snapshot)
            if (
                trade.trade_id != trade_id
                or trade.account_scope != self.fence.account_scope
            ):
                raise RuntimeError("canonical OMS trade identity/scope mismatch")
            if trade.state == TradeLifecycleState.POSITION_OPENED:
                open_trades[trade.trade_id] = trade
            elif trade.state == TradeLifecycleState.POSITION_CLOSED:
                closed_trades.append(trade)
            else:
                raise RuntimeError(
                    "canonical OMS trade "
                    f"{trade_id} is in unresolved state {trade.state.value}"
                )
        lifecycle._open_trades.clear()
        lifecycle._open_trades.update(open_trades)
        lifecycle._closed_trades = closed_trades
        return {"open": len(open_trades), "closed": len(closed_trades)}

    async def latest_snapshot(self, trade_id: str) -> dict[str, Any] | None:
        events = await self.oms.read_events(self.fence.account_scope, trade_id)
        for event in reversed(events):
            if event.payload.get("event_type") in {
                "TRADE_SNAPSHOT",
                "TRADE_COST_ADJUSTMENT",
            }:
                snapshot = event.payload.get("trade")
                if isinstance(snapshot, dict):
                    self._revisions[trade_id] = event.revision
                    self._last_event_ids[trade_id] = event.event_id
                    return snapshot
                legacy = event.payload.get("legacy_source")
                if isinstance(legacy, dict):
                    raw_payload = await self.oms.get_legacy_import_payload(
                        self.fence.account_scope,
                        str(legacy.get("table", "")),
                        str(legacy.get("fingerprint", "")),
                        legacy.get("occurrence"),
                        expected_trade_id=trade_id,
                        expected_updated_at=str(legacy.get("updated_at", "")),
                    )
                    import json

                    restored = json.loads(raw_payload)
                    if (
                        not isinstance(restored, dict)
                        or restored.get("trade_id") != trade_id
                    ):
                        raise RuntimeError(
                            "legacy OMS snapshot identity does not match its aggregate"
                        )
                    embedded_scope = restored.get("account_scope")
                    if embedded_scope and embedded_scope != self.fence.account_scope:
                        raise RuntimeError(
                            "legacy OMS snapshot account scope does not match its aggregate"
                        )
                    restored["account_scope"] = self.fence.account_scope
                    self._revisions[trade_id] = event.revision
                    self._last_event_ids[trade_id] = event.event_id
                    return restored
        self._revisions[trade_id] = events[-1].revision if events else 0
        return None


def bind_postgres_canonical_oms(
    lifecycle: Any,
    *,
    oms: PostgresCanonicalOMS,
    fence: WriterFence,
) -> PostgresLifecycleStateWriter:
    """Install the PostgreSQL-first writer after lifecycle projections are wired."""
    projection = getattr(lifecycle, "_execution_state_writer", None)
    if not callable(projection):
        raise RuntimeError(
            "configure the existing durable execution-state projection before OMS binding"
        )
    writer = PostgresLifecycleStateWriter(oms, fence, projection)
    lifecycle._execution_state_writer = writer
    lifecycle._postgres_canonical_oms_writer = writer
    return writer
