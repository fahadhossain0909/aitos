"""Low-cardinality market-data telemetry primitives.

Keep hot-path logging sampled/periodic. Counters and gauges are cheap and can
be exported by the existing health/audit layer later. The important invariant
is that every transport has the same vocabulary: received, parsed, published,
processed, rejected, stale, reconnects and queue depth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StreamTelemetry:
    name: str
    received: int = 0
    parsed: int = 0
    published: int = 0
    processed: int = 0
    rejected: int = 0
    stale: int = 0
    errors: int = 0
    reconnects: int = 0
    sequence_gaps: int = 0
    queue_waits: int = 0
    max_queue_depth: int = 0
    last_event_time_ms: int | None = None
    last_ingest_time_ms: int | None = None
    started_monotonic: float = field(default_factory=time.monotonic)

    def mark_received(self, event_time_ms: int | None = None) -> None:
        self.received += 1
        self.last_event_time_ms = event_time_ms
        self.last_ingest_time_ms = int(time.time() * 1000)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "received": self.received,
            "parsed": self.parsed,
            "published": self.published,
            "processed": self.processed,
            "rejected": self.rejected,
            "stale": self.stale,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "sequence_gaps": self.sequence_gaps,
            "queue_waits": self.queue_waits,
            "max_queue_depth": self.max_queue_depth,
            "last_event_time_ms": self.last_event_time_ms,
            "last_ingest_time_ms": self.last_ingest_time_ms,
            "age_seconds": (
                None
                if self.last_event_time_ms is None
                else max(0.0, time.time() - self.last_event_time_ms / 1000.0)
            ),
            "uptime_seconds": time.monotonic() - self.started_monotonic,
        }


class MarketDataTelemetry:
    """Process-local registry for transport and pipeline health snapshots."""

    def __init__(self) -> None:
        self.streams: dict[str, StreamTelemetry] = {}

    def stream(self, name: str) -> StreamTelemetry:
        telemetry = self.streams.get(name)
        if telemetry is None:
            telemetry = self.streams[name] = StreamTelemetry(name=name)
        return telemetry

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: metric.snapshot() for name, metric in self.streams.items()}
