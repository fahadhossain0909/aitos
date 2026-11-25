"""Crash-recoverable projection worker for the canonical PostgreSQL OMS outbox.

The PostgreSQL event journal remains authoritative. This worker rebuilds
legacy SQLite/ClickHouse projections from verified immutable events and uses
leased claims so a process crash cannot strand an outbox row permanently.
"""

from __future__ import annotations

import asyncio
import json
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
                except Exception as exc:  # noqa: BLE001 - preserve the durable retry record
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

        if payload.get("event_type") != "TRADE_SNAPSHOT":
            # Execution-intent and financial events are canonical write models;
            # they require no duplicate mutable projection in the legacy stores.
            return

        snapshot = payload.get("trade")
        if not isinstance(snapshot, dict):
            # Imported legacy snapshots are resolved only through immutable
            # source evidence and revalidated by the lifecycle adapter.
            snapshot = await self.lifecycle_writer.latest_snapshot(aggregate_id)
        if not isinstance(snapshot, dict):
            raise OMSIntegrityError("trade snapshot outbox event has no recoverable state")
        from aitos.trading.persistent_state import _trade_from_dict

        trade: Trade = _trade_from_dict(snapshot)
        if trade.trade_id != aggregate_id or trade.account_scope != scope:
            raise OMSIntegrityError("projected trade identity or account scope mismatch")
        await self.lifecycle_writer.projection_writer(trade)
