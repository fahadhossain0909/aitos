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
    depth: int = 0


class BoundedMarketQueue(Generic[T]):
    """A bounded async queue that never grows without limit."""

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
            self.stats.depth = self._queue.qsize()
            return False
        self.stats.accepted += 1
        self.stats.depth = self._queue.qsize()
        return True

    async def get(self) -> T:
        item = await self._queue.get()
        self.stats.depth = self._queue.qsize()
        return item

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        depth = self._queue.qsize()
        self.stats.depth = depth
        return depth

    def snapshot(self) -> dict[str, int]:
        depth = self._queue.qsize()
        self.stats.depth = depth
        return {
            "capacity": self.stats.capacity,
            "depth": depth,
            "accepted": self.stats.accepted,
            "dropped": self.stats.dropped,
        }
