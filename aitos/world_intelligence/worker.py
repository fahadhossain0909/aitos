"""Bounded WIATS ingestion worker with fenced leases and durable failure replay."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from aitos.world_intelligence.adapters import EventSink, IntelligenceSourceAdapter
from aitos.world_intelligence.entity_resolution import (
    DEFAULT_ENTITY_REGISTRY,
    EntityAliasRegistry,
)
from aitos.world_intelligence.lease import LeaseLost, LeaseUnavailable
from aitos.world_intelligence.pipeline import enrich_event_batch
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceRunResult:
    source_id: str
    event_count: int
    cursor: str | None
    error: str | None = None


class CursorReplayConflict(RuntimeError):
    """The durable source cursor no longer matches a replayable page boundary."""


class IntelligenceIngestionWorker:
    def __init__(
        self,
        adapters: Sequence[IntelligenceSourceAdapter],
        store: ClickHouseIntelligenceStore,
        sink: EventSink,
        *,
        poll_interval_seconds: float = 30.0,
        publisher_groups: dict[str, str] | None = None,
        entity_registry: EntityAliasRegistry = DEFAULT_ENTITY_REGISTRY,
        lease_manager: Any | None = None,
    ):
        if not adapters or poll_interval_seconds < 1.0:
            raise ValueError(
                "at least one adapter and a poll interval >= 1s are required"
            )
        source_ids = [adapter.source_id for adapter in adapters]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("only one adapter per source_id is allowed")
        self.adapters = tuple(adapters)
        self.store = store
        self.sink = sink
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.publisher_groups = dict(publisher_groups or {})
        self.entity_registry = entity_registry
        self.lease_manager = lease_manager
        self._stop = asyncio.Event()
        self._running = False
        self._metrics = {
            "pages_succeeded": 0,
            "events_persisted": 0,
            "events_published": 0,
            "pages_failed": 0,
            "dead_letters_written": 0,
            "dead_letters_replayed": 0,
            "dead_letters_exhausted": 0,
            "dead_letters_quarantined": 0,
            "lease_contention": 0,
            "lease_lost": 0,
        }

    def metrics_snapshot(self) -> dict[str, int]:
        """Return process-local monotonic counters for scrape/health integration."""
        return dict(self._metrics)

    async def _record_source_run(
        self,
        *,
        source_id: str,
        started_at: datetime,
        status: str,
        event_count: int,
        cursor_before: str | None,
        cursor_after: str | None,
        error_type: str = "",
        fencing_token: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        recorder = getattr(self.store, "record_source_run", None)
        if recorder is None:
            return
        try:
            await recorder(
                source_id=source_id,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                status=status,
                event_count=event_count,
                cursor_before=cursor_before,
                cursor_after=cursor_after,
                error_type=error_type,
                fencing_token=fencing_token,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.exception(
                "Unable to persist WIATS source-run telemetry: %s", source_id
            )

    async def source_run_summary(self, *, lookback_hours: float = 24.0):
        loader = getattr(self.store, "load_source_run_summary", None)
        if loader is None:
            return []
        return await loader(
            as_of=datetime.now(timezone.utc), lookback_hours=lookback_hours
        )

    async def _process_source(self, adapter: IntelligenceSourceAdapter, lease=None):
        source_id = adapter.source_id
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        cursor_before = None
        events = []
        next_cursor = None
        try:
            cursor_before = await self.store.load_cursor(source_id)
            events, next_cursor = await adapter.poll(cursor=cursor_before)
            enriched = enrich_event_batch(
                events,
                publisher_groups=self.publisher_groups,
                registry=self.entity_registry,
            )
            events = list(enriched.events)
            for quality in enriched.source_quality:
                if quality.suspected_automation:
                    logger.warning(
                        "source quality requires review: %s %s",
                        quality.source_id,
                        ",".join(quality.reasons),
                    )
            for cluster in enriched.coordination_clusters:
                logger.warning(
                    "cross-publisher coordination candidate requires review: "
                    "cluster=%s sources=%s shared_stories=%s",
                    cluster.cluster_id,
                    ",".join(cluster.source_ids),
                    len(cluster.shared_story_ids),
                )
            if lease is not None:
                await lease.assert_valid()
            persisted = await self.store.append_events(source_id, events)
            self._metrics["events_persisted"] += persisted
            for event in events:
                if lease is not None:
                    await lease.assert_valid()
                await self.sink.publish(
                    "intelligence.events.normalized",
                    {
                        "schema_version": event.schema_version,
                        "event_id": event.event_id,
                        "deduplication_key": event.deduplication_key,
                        "payload": event.to_dict(),
                    },
                )
                self._metrics["events_published"] += 1
            if lease is not None:
                await lease.assert_valid()
            if lease is None:
                await self.store.save_cursor(source_id, next_cursor)
            else:
                await self.store.save_cursor(
                    source_id, next_cursor, fencing_token=lease.fencing_token
                )
            self._metrics["pages_succeeded"] += 1
            await self._record_source_run(
                source_id=source_id,
                started_at=started_at,
                status="success",
                event_count=len(events),
                cursor_before=cursor_before,
                cursor_after=next_cursor,
                fencing_token=lease.fencing_token if lease is not None else 0,
                latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
            )
            return SourceRunResult(source_id, len(events), next_cursor)
        except Exception as exc:
            self._metrics["pages_failed"] += 1
            if isinstance(exc, LeaseLost):
                self._metrics["lease_lost"] += 1
            logger.exception("World-intelligence source poll failed: %s", source_id)
            await self._record_source_run(
                source_id=source_id,
                started_at=started_at,
                status="lease_lost" if isinstance(exc, LeaseLost) else "failed",
                event_count=len(events),
                cursor_before=cursor_before,
                cursor_after=next_cursor,
                error_type=type(exc).__name__,
                fencing_token=lease.fencing_token if lease is not None else 0,
                latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
            )
            # A provider poll failure has no immutable page to replay. Keep it
            # in logs/metrics; only persist DLQ entries after a page was fetched.
            if events or next_cursor is not None:
                try:
                    failure_id = await self.store.record_dead_letter(
                        source_id=source_id,
                        cursor_before=cursor_before,
                        cursor_after=next_cursor,
                        events=events,
                        error=exc,
                    )
                    self._metrics["dead_letters_written"] += 1
                    logger.error(
                        "WIATS page stored in dead-letter queue: %s", failure_id
                    )
                except Exception:
                    logger.exception(
                        "Unable to persist WIATS dead-letter page: %s", source_id
                    )
            return SourceRunResult(source_id, 0, None, type(exc).__name__)

    async def run_once(self) -> tuple[SourceRunResult, ...]:
        if self._running:
            raise RuntimeError("worker run_once is not reentrant")
        self._running = True
        results: list[SourceRunResult] = []
        try:
            for adapter in self.adapters:
                if self.lease_manager is None:
                    results.append(await self._process_source(adapter))
                    continue
                try:
                    async with self.lease_manager.hold(adapter.source_id) as lease:
                        results.append(await self._process_source(adapter, lease))
                except LeaseUnavailable:
                    self._metrics["lease_contention"] += 1
                    now = datetime.now(timezone.utc)
                    await self._record_source_run(
                        source_id=adapter.source_id,
                        started_at=now,
                        status="lease_unavailable",
                        event_count=0,
                        cursor_before=None,
                        cursor_after=None,
                        error_type="LeaseUnavailable",
                    )
                    results.append(
                        SourceRunResult(adapter.source_id, 0, None, "LeaseUnavailable")
                    )
                except Exception as exc:
                    self._metrics["lease_lost"] += 1
                    now = datetime.now(timezone.utc)
                    await self._record_source_run(
                        source_id=adapter.source_id,
                        started_at=now,
                        status="lease_lost",
                        event_count=0,
                        cursor_before=None,
                        cursor_after=None,
                        error_type=type(exc).__name__,
                    )
                    logger.exception(
                        "Unable to acquire or validate WIATS source lease: %s",
                        adapter.source_id,
                    )
                    results.append(
                        SourceRunResult(adapter.source_id, 0, None, type(exc).__name__)
                    )
        finally:
            self._running = False
        return tuple(results)

    async def replay_dead_letters(
        self, *, limit: int = 100, max_attempts: int = 5
    ) -> int:
        """Replay bounded pending pages; poison pages become terminal for review."""
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if self.lease_manager is None:
            raise RuntimeError(
                "dead-letter replay requires the distributed lease manager"
            )
        replayed = 0
        pending = await self.store.load_pending_dead_letters(limit=limit)
        for item in pending:
            source_id = item["source_id"]
            attempts = int(item["attempts"])
            if attempts >= max_attempts:
                await self.store.update_dead_letter(
                    item["failure_id"], status="exhausted", attempts=attempts
                )
                self._metrics["dead_letters_exhausted"] += 1
                continue
            try:
                async with self.lease_manager.hold(source_id) as lease:
                    events = item["events"]
                    await lease.assert_valid()
                    await self.store.append_events(source_id, events)
                    for event in events:
                        await lease.assert_valid()
                        await self.sink.publish(
                            "intelligence.events.normalized",
                            {
                                "schema_version": event.schema_version,
                                "event_id": event.event_id,
                                "deduplication_key": event.deduplication_key,
                                "payload": event.to_dict(),
                                "replayed": True,
                                "dead_letter_id": item["failure_id"],
                            },
                        )
                    await lease.assert_valid()
                    current_cursor = await self.store.load_cursor(source_id)
                    if current_cursor == item["cursor_before"]:
                        await self.store.save_cursor(
                            source_id,
                            item["cursor_after"],
                            fencing_token=lease.fencing_token,
                        )
                    elif current_cursor != item["cursor_after"]:
                        raise CursorReplayConflict(
                            "source cursor moved beyond or away from the dead-letter page boundary"
                        )
                    await self.store.update_dead_letter(
                        item["failure_id"],
                        status="replayed",
                        attempts=attempts + 1,
                    )
                    replayed += 1
                    self._metrics["dead_letters_replayed"] += 1
            except LeaseUnavailable:
                self._metrics["lease_contention"] += 1
                continue
            except LeaseLost:
                # Lease loss is infrastructure contention, not a poison page.
                # Keep the retry budget unchanged; any already-published events
                # are safe to redeliver because consumers deduplicate by event ID.
                self._metrics["lease_lost"] += 1
                continue
            except Exception as replay_error:
                logger.exception(
                    "WIATS dead-letter replay failed: %s", item["failure_id"]
                )
                try:
                    next_attempt = attempts + 1
                    terminal_status = (
                        "quarantined"
                        if isinstance(replay_error, CursorReplayConflict)
                        else "exhausted" if next_attempt >= max_attempts else "pending"
                    )
                    await self.store.update_dead_letter(
                        item["failure_id"],
                        status=terminal_status,
                        attempts=next_attempt,
                    )
                    if terminal_status == "quarantined":
                        self._metrics["dead_letters_quarantined"] += 1
                    elif terminal_status == "exhausted":
                        self._metrics["dead_letters_exhausted"] += 1
                except Exception:
                    logger.exception("Unable to update WIATS dead-letter retry state")
        return replayed

    async def run_forever(self) -> None:
        await self.store.ensure_schema()
        self._stop.clear()
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()


__all__ = [
    "SourceRunResult",
    "CursorReplayConflict",
    "IntelligenceIngestionWorker",
]
