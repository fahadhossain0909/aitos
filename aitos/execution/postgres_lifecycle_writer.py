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
        self._locks: dict[str, asyncio.Lock] = {}

    async def _revision(self, trade_id: str) -> int:
        if trade_id not in self._revisions:
            events = await self.oms.read_events(self.fence.account_scope, trade_id)
            self._revisions[trade_id] = events[-1].revision if events else 0
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
            )
            if event.revision != next_revision:
                raise RuntimeError(
                    "canonical OMS returned an unexpected aggregate revision"
                )
            self._revisions[trade_id] = next_revision
            # This is a projection only. If it fails, the committed canonical
            # event remains available for startup replay/reconciliation.
            await self.projection_writer(trade)

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
        aggregate_ids = await self.oms.list_aggregate_ids(self.fence.account_scope)
        for trade_id in aggregate_ids:
            snapshot = await self.latest_snapshot(trade_id)
            if snapshot is None:
                raise RuntimeError(
                    f"canonical OMS aggregate {trade_id} has no trade snapshot"
                )
            trade = _trade_from_dict(snapshot)
            if trade.trade_id != trade_id or trade.account_scope != self.fence.account_scope:
                raise RuntimeError("canonical OMS trade identity/scope mismatch")
            if trade.state == TradeLifecycleState.POSITION_OPENED:
                open_trades[trade.trade_id] = trade
            elif trade.state == TradeLifecycleState.POSITION_CLOSED:
                closed_trades.append(trade)
            else:
                raise RuntimeError(
                    f"canonical OMS trade {trade_id} is in unresolved state {trade.state.value}"
                )
        lifecycle._open_trades.clear()
        lifecycle._open_trades.update(open_trades)
        lifecycle._closed_trades = closed_trades
        return {"open": len(open_trades), "closed": len(closed_trades)}

    async def latest_snapshot(self, trade_id: str) -> dict[str, Any] | None:
        events = await self.oms.read_events(self.fence.account_scope, trade_id)
        for event in reversed(events):
            if event.payload.get("event_type") == "TRADE_SNAPSHOT":
                snapshot = event.payload.get("trade")
                if isinstance(snapshot, dict):
                    self._revisions[trade_id] = event.revision
                    return snapshot
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
