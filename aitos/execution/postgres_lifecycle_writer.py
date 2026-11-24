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
            if event.payload.get("event_type") == "TRADE_SNAPSHOT":
                snapshot = event.payload.get("trade")
                if isinstance(snapshot, dict):
                    self._revisions[trade_id] = event.revision
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
