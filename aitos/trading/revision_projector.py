"""Explicit bounded paper projector. No auto-start, client creation or DDL execution.

A sink insert timeout is ambiguous: never ACK, verify before any later retry.
This projects immutable evidence only; it cannot submit orders or fence venue IO.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

from aitos.projection_evidence import public_projection, unavailable
from aitos.trading.revision_journal import (
    JournalIntegrityError,
    Revision,
    RevisionConflict,
    RevisionJournal,
    Writer,
    verify_projection_batch,
)


class ProjectorBusy(RuntimeError):
    pass


class ClickHouseEvidenceSink:
    """Injected async client and pre-created fixed table. No credentials/config here."""

    def __init__(self, client):
        self.client = client

    async def lookup(self, events):
        if type(events) is not list or not 1 <= len(events) <= 100:
            raise ValueError("bounded nonempty lookup batch required")
        verify_projection_batch(events)
        scope = events[0].scope_key
        if any(event.scope_key != scope for event in events):
            raise ValueError("one trusted scope per lookup")
        # Scalar typed parameters are portable across the project's async client
        # and embedded-engine binding; no user values enter SQL text.
        parameters = {"scope": scope}
        terms = []
        for i, event in enumerate(events):
            parameters.update(
                {
                    f"trade{i}": event.trade_id,
                    f"revision{i}": event.revision,
                    f"event{i}": event.event_id,
                }
            )
            terms.append(
                f"(trade_id={{trade{i}:String}} AND revision={{revision{i}:UInt64}}) OR event_id={{event{i}:String}}"
            )
        predicate = " OR ".join(terms)
        # Only bounded numeric placeholder names enter this template. Every data
        # value is passed separately via typed ClickHouse parameters.
        sql = f"SELECT scope_key,trade_id,revision,event_id,writer_id,generation,payload_json,digest FROM prototype_revision_evidence WHERE scope_key={{scope:String}} AND ({predicate}) LIMIT 1001 SETTINGS max_execution_time=3,max_bytes_to_read=67108864,max_memory_usage=134217728,read_overflow_mode='throw',max_result_bytes=8388608,result_overflow_mode='throw'"  # nosec B608
        result = await self.client.query(sql, parameters=parameters)
        if len(result.result_rows) > 1000:
            raise JournalIntegrityError("sink evidence bound exceeded")
        return [Revision(*row) for row in result.result_rows]

    async def insert(self, events):
        if type(events) is not list or not 1 <= len(events) <= 100:
            raise ValueError("bounded nonempty insert batch required")
        verify_projection_batch(events)
        if any(event.scope_key != events[0].scope_key for event in events):
            raise ValueError("one trusted scope per insert")
        await self.client.insert(
            "prototype_revision_evidence",
            [list(event.__dict__.values()) for event in events],
            column_names=[
                "scope_key",
                "trade_id",
                "revision",
                "event_id",
                "writer_id",
                "generation",
                "payload_json",
                "digest",
            ],
        )


class RevisionProjector:
    def __init__(
        self,
        journal: RevisionJournal,
        writer: Writer,
        sink,
        *,
        batch_size=50,
        timeout=6,
    ):
        if (
            type(journal) is not RevisionJournal
            or type(writer) is not Writer
            or writer.scope.mode != "paper"
        ):
            raise ValueError("explicit paper journal/writer required")
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 100
            or type(timeout) not in (int, float)
            or not 0 < timeout <= 30
        ):
            raise ValueError("bounded projection settings required")
        self.journal = journal
        self.writer = writer
        self.sink = sink
        self.batch_size = batch_size
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.observation = unavailable("not_observed")

    def public_status(self):
        return public_projection(copy.deepcopy(self.observation))

    async def _lookup(self, batch):
        rows = await asyncio.wait_for(self.sink.lookup(batch), self.timeout)
        if type(rows) is not list or len(rows) + len(batch) > 1000:
            raise JournalIntegrityError("bounded sink records required")
        revision_keys = {(e.scope_key, e.trade_id, e.revision) for e in batch}
        event_keys = {(e.scope_key, e.event_id) for e in batch}
        for row in rows:
            if type(row) is not Revision or (
                (row.scope_key, row.trade_id, row.revision) not in revision_keys
                and (row.scope_key, row.event_id) not in event_keys
            ):
                raise JournalIntegrityError("unexpected sink evidence")
        verify_projection_batch([*batch, *rows])
        return rows

    async def run_once(self):
        """One explicit batch only. All errors sanitized in public observations."""
        if self.lock.locked():
            raise ProjectorBusy("projector already running")
        async with self.lock:
            stamp = lambda: datetime.now(timezone.utc).isoformat()
            self.observation = unavailable("operational_error", "running", stamp())
            try:
                if not await asyncio.to_thread(self.journal.is_current, self.writer):
                    raise RevisionConflict("writer fenced")
                batch = await asyncio.to_thread(
                    self.journal.pending, self.writer.scope, limit=self.batch_size
                )
                inserted = 0
                if batch:
                    existing = await self._lookup(batch)
                    missing = [event for event in batch if event not in existing]
                    # Recheck before side effects; a takeover during an in-flight
                    # sink request may still leave immutable duplicate evidence.
                    if not await asyncio.to_thread(
                        self.journal.is_current, self.writer
                    ):
                        raise RevisionConflict("writer fenced")
                    if missing:
                        await asyncio.wait_for(self.sink.insert(missing), self.timeout)
                        inserted = len(missing)
                    observed = await self._lookup(batch)
                    if any(event not in observed for event in batch):
                        raise JournalIntegrityError(
                            "sink did not confirm complete batch"
                        )
                    await asyncio.to_thread(
                        self.journal.acknowledge_batch, self.writer, batch
                    )
                counts = await asyncio.to_thread(
                    self.journal.outbox_counts, self.writer.scope
                )
                if not await asyncio.to_thread(self.journal.is_current, self.writer):
                    raise RevisionConflict("writer fenced")
                self.observation = dict(
                    schema_version=1,
                    scope="local_projection_observation",
                    available=True,
                    status="verified" if batch else "empty",
                    reason="none",
                    observed_at=stamp(),
                    counts=counts,
                    last_batch=dict(
                        checked=len(batch), inserted=inserted, acknowledged=len(batch)
                    ),
                    source="local_journal",
                    controls="read_only",
                    live_authorized=False,
                    production_certified=False,
                )
            except asyncio.CancelledError:
                self.observation = unavailable(
                    "operational_error", "cancelled", stamp()
                )
                raise
            except asyncio.TimeoutError:
                self.observation = unavailable("operational_error", "timeout", stamp())
            except RevisionConflict:
                self.observation = unavailable("operational_error", "fenced", stamp())
            except JournalIntegrityError:
                self.observation = unavailable("operational_error", "conflict", stamp())
            except Exception:
                self.observation = unavailable("operational_error", "failed", stamp())
            return self.public_status()
