"""Bounded queue primitive with explicit overflow telemetry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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

    def put_latest_nowait(
        self,
        item: T,
        *,
        can_replace: Callable[[T], bool] | None = None,
    ) -> bool:
        """Enqueue immediately, replacing the oldest eligible item when full.

        A caller can protect sequence-critical state (for example order-book
        snapshots/deltas) from being evicted by a latest-wins ticker/trade burst.
        If every queued item is protected, reject the incoming item explicitly.
        This operation never waits for a slow downstream consumer.
        """
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            replaceable = can_replace or (lambda _queued: True)
            queue_items = self._queue._queue
            victim = next(
                (queued for queued in queue_items if replaceable(queued)), None
            )
            if victim is None:
                self.stats.dropped += 1
                self.stats.depth = self._queue.qsize()
                return False
            queue_items.remove(victim)
            self._queue.task_done()
            self.stats.dropped += 1
            self.stats.replaced_oldest += 1
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
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
            "replaced_oldest": self.stats.replaced_oldest,
        }
