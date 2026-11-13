"""Provider-neutral interfaces; concrete adapters must honor provider terms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from aitos.world_intelligence.contracts import IntelligenceEvent


class IntelligenceSourceAdapter(Protocol):
    """Bounded, resumable source adapter that emits normalized events."""

    source_id: str

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        """Return one bounded page and an opaque continuation cursor."""
        ...


class EventSink(Protocol):
    """Transport-neutral publisher; production wiring uses the existing Redis bus."""

    async def publish(self, topic: str, payload: Mapping[str, object]) -> None: ...


async def ingest_page(
    adapter: IntelligenceSourceAdapter,
    sink: EventSink,
    *,
    cursor: str | None = None,
) -> str | None:
    """Publish a bounded page with deterministic source-story identity."""
    events, next_cursor = await adapter.poll(cursor=cursor)
    for event in events:
        await sink.publish(
            "intelligence.events.normalized",
            {
                "schema_version": event.schema_version,
                "event_id": event.event_id,
                "deduplication_key": event.deduplication_key,
                "payload": event.to_dict(),
            },
        )
    return next_cursor
