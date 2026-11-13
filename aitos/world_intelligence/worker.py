"""Single-active-instance, graceful, bounded intelligence ingestion worker."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

from aitos.world_intelligence.adapters import EventSink, IntelligenceSourceAdapter
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore
from aitos.world_intelligence.entity_resolution import (
    DEFAULT_ENTITY_REGISTRY,
    EntityAliasRegistry,
)
from aitos.world_intelligence.pipeline import enrich_event_batch

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
        self._stop = asyncio.Event()
        self._running = False

    async def run_once(self) -> tuple[SourceRunResult, ...]:
        if self._running:
            raise RuntimeError("worker run_once is not reentrant")
        self._running = True
        results: list[SourceRunResult] = []
        try:
            for adapter in self.adapters:
                try:
                    cursor = await self.store.load_cursor(adapter.source_id)
                    events, next_cursor = await adapter.poll(cursor=cursor)
                    enriched = enrich_event_batch(
                        events,
                        publisher_groups=self.publisher_groups,
                        entity_registry=self.entity_registry,
                    )
                    events = list(enriched.events)
                    await self.store.append_events(adapter.source_id, events)
                    for event in events:
                        await self.sink.publish(
                            "intelligence.events.normalized",
                            {
                                "schema_version": event.schema_version,
                                "event_id": event.event_id,
                                "deduplication_key": event.deduplication_key,
                                "payload": event.to_dict(),
                            },
                        )
                    await self.store.save_cursor(adapter.source_id, next_cursor)
                    results.append(
                        SourceRunResult(adapter.source_id, len(events), next_cursor)
                    )
                except Exception as exc:
                    logger.exception(
                        "World-intelligence source poll failed: %s", adapter.source_id
                    )
                    results.append(
                        SourceRunResult(adapter.source_id, 0, None, type(exc).__name__)
                    )
        finally:
            self._running = False
        return tuple(results)

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
