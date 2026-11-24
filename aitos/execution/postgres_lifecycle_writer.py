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
            raise ValueError("TradeLifecycle writer requires a persisted Trade aggregate")
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
                raise RuntimeError("canonical OMS returned an unexpected aggregate revision")
            self._revisions[trade_id] = next_revision
            # This is a projection only. If it fails, the committed canonical
            # event remains available for startup replay/reconciliation.
            await self.projection_writer(trade)

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
