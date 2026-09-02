"""End-to-end market-data latency attribution telemetry.

This module is intentionally observational: it does not drop, reorder, or
modify market-data events. It provides a correlation id and stage timestamps
so production audits can identify where source age first increases.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class MarketDataTrace:
    """Lightweight per-event trace shared across pipeline stages."""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str = ""
    trade_id: int | None = None
    source_event_ms: int | None = None
    stages: dict[str, str] = field(default_factory=dict)
    durations_ms: dict[str, float] = field(default_factory=dict)

    def mark(self, stage: str) -> None:
        self.stages[stage] = datetime.now(timezone.utc).isoformat()

    def mark_source(self, event_ms: int | None) -> None:
        self.source_event_ms = event_ms
        self.mark("ws_received")

    def source_age_ms(self) -> float | None:
        if self.source_event_ms is None:
            return None
        return max(
            0.0, datetime.now(timezone.utc).timestamp() * 1000.0 - self.source_event_ms
        )

    def log_context(self, **extra: Any) -> dict[str, Any]:
        context: dict[str, Any] = {
            "trace_id": self.trace_id,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "source_event_ms": self.source_event_ms,
            "source_age_ms": self.source_age_ms(),
            "stages": dict(self.stages),
            "durations_ms": dict(self.durations_ms),
        }
        context.update(extra)
        return context


def monotonic_ms() -> float:
    """Monotonic milliseconds for local processing intervals."""

    return time.monotonic() * 1000.0
