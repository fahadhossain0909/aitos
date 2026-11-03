"""Explicit REST recovery policy for the canonical market-data plane."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from .contracts import MarketEvent, MarketSource


def recoverable_events(
    events: Iterable[MarketEvent], *, max_age_seconds: float = 60.0
) -> list[MarketEvent]:
    """Return recoverable REST events marked degraded; never relabel them live.

    WebSocket events pass through unchanged. REST events older than the recovery
    window are discarded so a long outage cannot inject ancient state into the
    live scanner.
    """
    now = datetime.now(timezone.utc)
    result: list[MarketEvent] = []
    for event in events:
        if event.source is not MarketSource.REST:
            result.append(event)
            continue
        age = max(0.0, (now - event.event_time).total_seconds())
        if age > max_age_seconds:
            continue
        result.append(event)
    return result
