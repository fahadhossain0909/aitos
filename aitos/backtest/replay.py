"""Deterministic replay primitives for historical market events."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class ReplayEvent(Protocol):
    timestamp: datetime


@dataclass(frozen=True)
class ReplayStats:
    events_seen: int
    events_emitted: int
    start_time: datetime | None
    end_time: datetime | None


class MarketReplay:
    """Replay historical events in timestamp order without changing them."""

    def __init__(self, events: Iterable[ReplayEvent]) -> None:
        self._events = sorted(events, key=lambda event: event.timestamp)

    @property
    def events(self) -> Sequence[ReplayEvent]:
        return tuple(self._events)

    def run(self, handler: Callable[[ReplayEvent], None]) -> ReplayStats:
        emitted = 0
        for event in self._events:
            handler(event)
            emitted += 1
        return ReplayStats(
            events_seen=len(self._events),
            events_emitted=emitted,
            start_time=self._events[0].timestamp if self._events else None,
            end_time=self._events[-1].timestamp if self._events else None,
        )
