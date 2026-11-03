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
    replaced_oldest: int = 0


class BoundedMarketQueue(Generic[T]):
    """A bounded queue with latest-wins overflow semantics for live data."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)
        self.stats = QueueStats(capacity=capacity)

    def put_nowait(self, item: T) -> bool:
        """Enqueue without blocking; reject when full for compatibility."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            self.stats.depth = self._queue.qsize()
            return False
        self.stats.accepted += 1
        self.stats.depth = self._queue.qsize()
        return True

    def put_latest_nowait(self, item: T) -> bool:
        """Enqueue immediately, replacing the oldest item when full.

        Live trading must prefer the newest market state over an old backlog.
        This operation never waits for a slow downstream consumer.
        """
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                return False
            self.stats.dropped += 1
            self.stats.replaced_oldest += 1
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
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
            "replaced_oldest": self.stats.replaced_oldest,
        }
