"""Bounded WIATS ingestion worker with fenced leases and durable failure replay."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from aitos.world_intelligence.adapters import EventSink, IntelligenceSourceAdapter
from aitos.world_intelligence.entity_resolution import DEFAULT_ENTITY_REGISTRY, EntityAliasRegistry
from aitos.world_intelligence.lease import LeaseUnavailable
from aitos.world_intelligence.pipeline import enrich_event_batch
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceRunResult:
    source_id: str
    event_count: int
    cursor: str | None
    error: str | None = None


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
            raise ValueError("at least one adapter and a poll interval >= 1s are required")
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
            "lease_contention": 0,
            "lease_lost": 0,
        }

    def metrics_snapshot(self) -> dict[str, int]:
        """Return process-local monotonic counters for scrape/health integration."""
        return dict(self._metrics)

    async def _process_source(self, adapter: IntelligenceSourceAdapter, lease=None):
        source_id = adapter.source_id
        cursor_before = await self.store.load_cursor(source_id)
        events = []
        next_cursor = None
        try:
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
            await self.store.save_cursor(
                source_id,
                next_cursor,
                fencing_token=lease.fencing_token if lease is not None else 0,
            )
            self._metrics["pages_succeeded"] += 1
            return SourceRunResult(source_id, len(events), next_cursor)
        except Exception as exc:
            self._metrics["pages_failed"] += 1
            if type(exc).__name__ == "LeaseLost":
                self._metrics["lease_lost"] += 1
            logger.exception("World-intelligence source poll failed: %s", source_id)
            try:
                failure_id = await self.store.record_dead_letter(
                    source_id=source_id,
                    cursor_before=cursor_before,
                    cursor_after=next_cursor,
                    events=events,
                    error=exc,
                )
                self._metrics["dead_letters_written"] += 1
                logger.error("WIATS page stored in dead-letter queue: %s", failure_id)
            except Exception:
                logger.exception("Unable to persist WIATS dead-letter page: %s", source_id)
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
                    results.append(
                        SourceRunResult(adapter.source_id, 0, None, "LeaseUnavailable")
                    )
        finally:
            self._running = False
        return tuple(results)

    async def replay_dead_letters(self, *, limit: int = 100) -> int:
        """Explicitly replay pending pages; never polls providers or bypasses leases."""
        if self.lease_manager is None:
            raise RuntimeError("dead-letter replay requires the distributed lease manager")
        replayed = 0
        pending = await self.store.load_pending_dead_letters(limit=limit)
        for item in pending:
            source_id = item["source_id"]
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
                    await self.store.save_cursor(
                        source_id,
                        item["cursor_after"],
                        fencing_token=lease.fencing_token,
                    )
                    await self.store.update_dead_letter(
                        item["failure_id"],
                        status="replayed",
                        attempts=item["attempts"] + 1,
                    )
                    replayed += 1
                    self._metrics["dead_letters_replayed"] += 1
            except LeaseUnavailable:
                self._metrics["lease_contention"] += 1
                continue
            except Exception:
                logger.exception(
                    "WIATS dead-letter replay failed: %s", item["failure_id"]
                )
                try:
                    await self.store.update_dead_letter(
                        item["failure_id"],
                        status="pending",
                        attempts=item["attempts"] + 1,
                    )
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


__all__ = ["SourceRunResult", "IntelligenceIngestionWorker"]
