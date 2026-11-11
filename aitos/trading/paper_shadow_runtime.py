"""Opt-in paper runner mirror. Legacy recovery remains authoritative.

No extra Redis consumer, no DDL, no venue access. Any mirror failure latches
unavailable evidence until restart; it never changes risk/exit decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from aitos.projection_evidence import unavailable
from aitos.trading.paper_revision import (
    PaperRevisionStore,
    decode_trade,
    encode_trade,
    timestamp,
)
from aitos.trading.revision_journal import RevisionConflict, canonical
from aitos.trading.revision_projector import RevisionProjector


class PaperShadowRuntime:
    def __init__(self, store: PaperRevisionStore, sink, *, interval=2):
        if type(interval) not in (int, float) or not 0.05 <= interval <= 30:
            raise ValueError("bounded interval required")
        self.store = store
        self.projector = RevisionProjector(store.journal, store.writer, sink)
        self.interval = interval
        self._failed = False
        self._closing = False
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def public_status(self):
        if self._failed:
            return unavailable(
                "operational_error", "failed", datetime.now(timezone.utc).isoformat()
            )
        if self._task is not None and self._task.done() and not self._stop.is_set():
            return unavailable(
                "operational_error", "failed", datetime.now(timezone.utc).isoformat()
            )
        return self.projector.public_status()

    async def record(self, trade):
        if self._failed or self._closing:
            return False
        try:
            # Detach before BOTH lock and thread boundaries. Lifecycle Trades
            # (including nested consensus data) can mutate while queued.
            payload = encode_trade(trade)
            snapshot = decode_trade(payload)
        except Exception:
            self._failed = True
            return False
        async with self._lock:
            if self._failed or self._closing:
                return False
            try:

                def write():
                    journal, writer = self.store.journal, self.store.writer
                    if not journal.is_current(writer):
                        raise RevisionConflict("writer fenced")
                    head = journal.head(writer.scope, snapshot.trade_id)
                    event_id = (
                        "snapshot-"
                        + hashlib.sha256(canonical(payload).encode()).hexdigest()
                    )
                    # Recognize an exact historical redelivery before chronology
                    # checks; append's immutable event identity cannot move heads.
                    previous = journal.find_event(writer.scope, event_id)
                    if (
                        head
                        and not previous
                        and timestamp(payload["updated_at"])
                        < timestamp(head.payload["updated_at"])
                    ):
                        raise RevisionConflict("out-of-order shadow evidence")
                    self.store.save_trade(
                        snapshot,
                        event_id=event_id,
                        expected_revision=head.revision if head else 0,
                    )

                io_task = asyncio.create_task(asyncio.to_thread(write))
                try:
                    await asyncio.shield(io_task)
                except asyncio.CancelledError:
                    # Drain the real SQLite operation before releasing the lock.
                    # Cancelling to_thread alone does not stop its native thread.
                    while True:
                        try:
                            await asyncio.shield(io_task)
                            break
                        except asyncio.CancelledError:
                            if io_task.cancelled():
                                # Global loop shutdown may cancel every task,
                                # including this one. Outcome is then unknown;
                                # fail closed, never spin on a terminal Future.
                                self._failed = True
                                break
                            # Further cancellation requests still must not cancel
                            # the Future standing in for the native SQLite write.
                            continue
                        except Exception:
                            self._failed = True
                            break
                    raise
                return True
            except asyncio.CancelledError:
                self._failed = True
                raise
            except Exception:
                # Explicit shadow degradation, never a false successful report.
                self._failed = True
                return False

    async def start(self):
        if self._task is not None:
            raise RuntimeError("shadow runtime already started")
        if self._stop.is_set():
            raise RuntimeError("closed runtime cannot restart")
        # Query preflight needs a nonempty batch; pending projection errors are
        # exposed by run_once. Table provisioning is an operator/CI responsibility.
        self._task = asyncio.create_task(self._run(), name="paper-revision-shadow")

    async def _run(self):
        while not self._stop.is_set():
            if not self._failed:
                report = await self.projector.run_once()
                if report["status"] in {"fenced", "conflict", "failed"}:
                    self._failed = True
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._closing = True
        self._stop.set()
        async with self._lock:
            pass  # Await any in-flight local write before closing dependent IO.
        if self._task is not None:
            # Do not cancel a SQLite thread mid-commit or close its client first.
            try:
                await self._task
            except Exception:
                self._failed = True


class MirroredPaperStateStore:
    """Only lifecycle writes mirrored; all recovery/financial reads stay legacy."""

    def __init__(self, legacy, runtime):
        self.legacy, self.runtime = legacy, runtime

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    async def save_trade(self, trade):
        await self.legacy.save_trade(trade)
        await self.runtime.record(trade)

    async def save_closed_trade(self, trade):
        await self.legacy.save_closed_trade(trade)
        await self.runtime.record(trade)


def configured_shadow(environment, client):
    """Explicit CAS generation; never silently steal ownership on restart."""
    enabled = environment.get("AITOS_PAPER_REVISION_SHADOW", "0")
    if enabled == "0":
        return None
    if enabled != "1":
        raise ValueError("shadow flag must be 0 or 1")
    from pathlib import Path

    from aitos.trading.revision_journal import RevisionJournal, Scope
    from aitos.trading.revision_projector import ClickHouseEvidenceSink

    if len(environment.get("AITOS_CONSOLE_TOKEN", "")) < 32:
        raise ValueError("protected observation required for shadow mode")
    if client is None:
        raise ValueError("initialized sink client required")
    path = Path(environment["AITOS_PAPER_REVISION_PATH"])
    if not path.is_absolute():
        raise ValueError("absolute local journal path required")
    scope = Scope(
        "paper",
        environment["AITOS_PAPER_REVISION_PROVIDER"],
        environment["AITOS_PAPER_REVISION_ACCOUNT"],
        environment["AITOS_PAPER_REVISION_RUN"],
    )
    generation = environment["AITOS_PAPER_REVISION_EXPECTED_GENERATION"]
    if not generation.isascii() or not generation.isdecimal():
        raise ValueError("explicit generation required")
    journal = RevisionJournal(path)
    writer = journal.acquire(
        scope,
        environment["AITOS_PAPER_REVISION_WRITER"],
        expected_generation=int(generation),
    )
    return PaperShadowRuntime(
        PaperRevisionStore(journal, writer), ClickHouseEvidenceSink(client)
    )
