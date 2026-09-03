"""Bounded queue primitive with explicit overflow telemetry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class QueueStats:
    capacity: int
    accepted: int = 0
    dropped: int = 0

    @property
    def depth(self) -> int:
        return 0


class BoundedMarketQueue(Generic[T]):
    """A bounded async queue that never grows without limit.

    ``put_nowait`` is intentionally used at the gateway boundary so a slow
    downstream consumer cannot silently turn market-data pressure into an
    unbounded memory backlog.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)
        self.stats = QueueStats(capacity=capacity)

    def put_nowait(self, item: T) -> bool:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            return False
        self.stats.accepted += 1
        return True

    async def get(self) -> T:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> dict[str, int]:
        return {
            "capacity": self.stats.capacity,
            "depth": self._queue.qsize(),
            "accepted": self.stats.accepted,
            "dropped": self.stats.dropped,
        }
