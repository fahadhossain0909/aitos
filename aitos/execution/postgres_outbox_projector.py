"""Crash-recoverable projection worker for the canonical PostgreSQL OMS outbox.

The PostgreSQL event journal remains authoritative. This worker rebuilds
legacy SQLite/ClickHouse projections from verified immutable events and uses
leased claims so a process crash cannot strand an outbox row permanently.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aitos.execution.postgres_lifecycle_writer import PostgresLifecycleStateWriter
from aitos.execution.postgres_oms import OMSIntegrityError, PostgresCanonicalOMS
from aitos.models.trade import Trade


class PostgresOMSOutboxProjector:
    """Replay pending canonical events into configured read-model projections."""

    def __init__(
        self,
        oms: PostgresCanonicalOMS,
        lifecycle_writer: PostgresLifecycleStateWriter,
        *,
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: int = 60,
        projection_reader: Callable[[], Awaitable[Iterable[Any]]] | None = None,
    ) -> None:
        if not isinstance(oms, PostgresCanonicalOMS):
            raise TypeError("PostgresCanonicalOMS is required")
        if not isinstance(lifecycle_writer, PostgresLifecycleStateWriter):
            raise TypeError("PostgresLifecycleStateWriter is required")
        if not worker_id or len(worker_id) > 192:
            raise ValueError("bounded outbox worker_id is required")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if oms is not lifecycle_writer.oms:
            raise ValueError("outbox worker and lifecycle writer must share one OMS")
        self.oms = oms
        self.lifecycle_writer = lifecycle_writer
        self.worker_id = worker_id
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.projection_reader = projection_reader
        self._run_lock = asyncio.Lock()

    async def run_once(self) -> dict[str, int]:
        """Project one leased batch; failed rows are released for bounded retry."""
        if self._run_lock.locked():
            return {"claimed": 0, "projected": 0, "failed": 0}
        async with self._run_lock:
            records = await self.oms.claim_outbox_leased(
                self.worker_id,
                limit=self.batch_size,
                lease_seconds=self.lease_seconds,
            )
            projected = 0
            failed = 0
            for record in records:
                token = str(record["claim_token"])
                try:
                    await self._verify_and_project(record)
                    await self.oms.acknowledge_outbox_claim(
                        record, owner_id=self.worker_id, claim_token=token
                    )
                    projected += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await self.oms.release_outbox_claim(
                        record,
                        owner_id=self.worker_id,
                        claim_token=token,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            return {
                "claimed": len(records),
                "projected": projected,
                "failed": failed,
            }

    async def run_until_drained(self, *, max_records: int = 100000) -> dict[str, int]:
        """Drain the currently pending scope outbox with a strict work bound."""
        if type(max_records) is not int or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be between 1 and 1,000,000")
        total_claimed = 0
        total_projected = 0
        total_failed = 0
        while total_claimed < max_records:
            remaining = min(self.batch_size, max_records - total_claimed)
            records = await self.oms.claim_outbox_leased(
                self.worker_id,
                limit=remaining,
                lease_seconds=self.lease_seconds,
            )
            if not records:
                break
            projected = 0
            failed = 0
            for record in records:
                token = str(record["claim_token"])
                try:
                    await self._verify_and_project(record)
                    await self.oms.acknowledge_outbox_claim(
                        record, owner_id=self.worker_id, claim_token=token
                    )
                    projected += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await self.oms.release_outbox_claim(
                        record,
                        owner_id=self.worker_id,
                        claim_token=token,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            total_claimed += len(records)
            total_projected += projected
            total_failed += failed
            if failed:
                break
        if total_claimed >= max_records:
            pending = await self.oms.claim_outbox_leased(
                self.worker_id,
                limit=1,
                lease_seconds=self.lease_seconds,
            )
            if pending:
                record = pending[0]
                await self.oms.release_outbox_claim(
                    record,
                    owner_id=self.worker_id,
                    claim_token=str(record["claim_token"]),
                    error="projection rebuild exceeded configured record bound",
                )
                raise RuntimeError(
                    "canonical projection rebuild exceeded its record bound"
                )
        return {
            "claimed": total_claimed,
            "projected": total_projected,
            "failed": total_failed,
        }

    async def audit_scope(self) -> dict[str, Any]:
        """Compare every canonical Trade snapshot with its durable read model."""
        if self.projection_reader is None:
            raise RuntimeError(
                "projection_reader is required for read-model parity audit"
            )
        projected_rows = await self.projection_reader()
        if not isinstance(projected_rows, Iterable):
            raise TypeError(
                "projection reader must return an iterable of Trade records"
            )
        projected: dict[str, str] = {}
        duplicate_projection_ids: set[str] = set()
        for trade in projected_rows:
            trade_id = str(getattr(trade, "trade_id", "")).strip()
            scope = str(getattr(trade, "account_scope", "") or "")
            to_dict = getattr(trade, "to_dict", None)
            if (
                not trade_id
                or scope != self.lifecycle_writer.fence.account_scope
                or not callable(to_dict)
            ):
                raise OMSIntegrityError(
                    "projection reader returned invalid Trade identity"
                )
            if trade_id in projected:
                duplicate_projection_ids.add(trade_id)
            projected[trade_id] = json.dumps(
                to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )

        canonical: dict[str, str] = {}
        scope = self.lifecycle_writer.fence.account_scope
        for aggregate_id in await self.oms.list_aggregate_ids(scope):
            events = await self.oms.read_events(scope, aggregate_id)
            snapshot_event = next(
                (
                    event
                    for event in reversed(events)
                    if item.payload.get("event_type")
                    in {
                        "TRADE_SNAPSHOT",
                        "TRADE_COST_ADJUSTMENT",
                    }
                    and isinstance(item.payload.get("trade"), dict)
                ),
                None,
            )
            if snapshot_event is None:
                continue
            snapshot = await self.lifecycle_writer.latest_snapshot(aggregate_id)
            if not isinstance(snapshot, dict):
                raise OMSIntegrityError(
                    f"canonical Trade aggregate {aggregate_id} has no recoverable snapshot"
                )
            from aitos.trading.persistent_state import _trade_from_dict

            trade = _trade_from_dict(snapshot)
            if trade.trade_id != aggregate_id or trade.account_scope != scope:
                raise OMSIntegrityError(
                    "canonical projection audit scope/identity mismatch"
                )
            canonical[aggregate_id] = json.dumps(
                trade.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )

        expected_ids = set(canonical)
        actual_ids = set(projected)
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        mismatched = sorted(
            trade_id
            for trade_id in expected_ids & actual_ids
            if canonical[trade_id] != projected[trade_id]
        )
        return {
            "canonical_count": len(canonical),
            "projection_count": len(projected),
            "missing_trade_ids": missing,
            "unexpected_trade_ids": unexpected,
            "mismatched_trade_ids": mismatched,
            "duplicate_projection_trade_ids": sorted(duplicate_projection_ids),
            "in_sync": not (
                missing or unexpected or mismatched or duplicate_projection_ids
            ),
        }

    async def rebuild_scope(
        self, *, confirm: bool = False, max_records: int = 100000
    ) -> dict[str, Any]:
        """Explicitly replay canonical Trade snapshots and verify read-model parity."""
        if confirm is not True:
            raise ValueError(
                "explicit full projection rebuild confirmation is required"
            )
        if self.projection_reader is None:
            raise RuntimeError(
                "projection_reader is required for full rebuild verification"
            )
        requeued = await self.oms.requeue_projection_outbox(
            self.lifecycle_writer.fence,
            confirm=True,
        )
        replay = await self.run_until_drained(max_records=max_records)
        if replay["failed"]:
            raise RuntimeError(
                f"canonical projection rebuild had {replay['failed']} failed records"
            )
        audit = await self.audit_scope()
        if not audit["in_sync"]:
            raise RuntimeError(
                "canonical projection rebuild completed with unresolved parity drift: "
                f"missing={len(audit['missing_trade_ids'])}, "
                f"unexpected={len(audit['unexpected_trade_ids'])}, "
                f"mismatched={len(audit['mismatched_trade_ids'])}"
            )
        return {"requeued": requeued, **replay, "audit": audit}

    async def audit_and_repair_scope(
        self, *, max_records: int = 100000
    ) -> dict[str, Any]:
        """Automatically rebuild only when canonical/projection parity has drifted."""
        before = await self.audit_scope()
        if before["in_sync"]:
            return {"repaired": False, "before": before, "after": before}
        rebuilt = await self.rebuild_scope(confirm=True, max_records=max_records)
        return {
            "repaired": True,
            "before": before,
            "rebuild": rebuilt,
            "after": rebuilt["audit"],
        }

    async def _verify_and_project(self, record: dict[str, Any]) -> None:
        scope = str(record["account_scope"])
        aggregate_id = str(record["aggregate_id"])
        revision = int(record["revision"])
        events = await self.oms.read_events(scope, aggregate_id)
        event = next((item for item in events if item.revision == revision), None)
        if event is None or event.event_hash != record["event_hash"]:
            raise OMSIntegrityError("outbox event does not match canonical hash chain")
        payload = json.loads(record["payload_json"])
        if payload != event.payload:
            raise OMSIntegrityError("outbox payload differs from canonical event")

        if payload.get("event_type") not in {
            "TRADE_SNAPSHOT",
            "TRADE_COST_ADJUSTMENT",
        } or not isinstance(payload.get("trade"), dict):
            # Intent and financial-only events have no Trade read-model payload.
            return

        # A delayed/expired worker may be replaying an older snapshot after a
        # newer revision was already projected. Always materialize the latest
        # canonical Trade snapshot so a stale worker cannot roll a projection back.
        latest_trade_event = next(
            (
                item
                for item in reversed(events)
                if item.payload.get("event_type") in {
                    "TRADE_SNAPSHOT",
                    "TRADE_COST_ADJUSTMENT",
                }
                and isinstance(item.payload.get("trade"), dict)
            ),
            None,
        )
        snapshot = (
            latest_trade_event.payload.get("trade")
            if latest_trade_event is not None
            else None
        )
        if not isinstance(snapshot, dict):
            # Imported legacy snapshots are resolved only through immutable
            # source evidence and revalidated by the lifecycle adapter.
            snapshot = await self.lifecycle_writer.latest_snapshot(aggregate_id)
        if not isinstance(snapshot, dict):
            raise OMSIntegrityError(
                "trade snapshot outbox event has no recoverable state"
            )
        from aitos.trading.persistent_state import _trade_from_dict

        trade: Trade = _trade_from_dict(snapshot)
        if trade.trade_id != aggregate_id or trade.account_scope != scope:
            raise OMSIntegrityError(
                "projected trade identity or account scope mismatch"
            )
        await self.lifecycle_writer.projection_writer(trade)
