"""Dynamically adjustable async concurrency gate for optional compute work."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class ConcurrencySnapshot:
    limit: int
    active: int
    waiting: int


class AdaptiveConcurrencyGate:
    """A loop-local gate whose limit can shrink without cancelling active work.

    Lowering the limit never interrupts an in-flight operation. New work waits
    until active work falls below the new limit. A zero limit pauses admission;
    callers must keep protected services outside this gate.
    """

    def __init__(self, max_concurrency: int, *, initial_limit: int | None = None):
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        limit = max_concurrency if initial_limit is None else initial_limit
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= max_concurrency
        ):
            raise ValueError("initial_limit must be between zero and max_concurrency")
        self.max_concurrency = max_concurrency
        self._limit = limit
        self._active = 0
        self._waiting = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    async def set_limit(self, limit: int) -> None:
        """Change future admission without cancelling currently active work."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 <= limit <= self.max_concurrency
        ):
            raise ValueError("limit must be between zero and max_concurrency")
        async with self._condition:
            self._limit = limit
            self._condition.notify_all()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire one slot and always release it, including on cancellation."""
        acquired = False
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: self._limit > 0 and self._active < self._limit
                )
                self._active += 1
                acquired = True
            finally:
                self._waiting -= 1
        try:
            yield
        finally:
            if acquired:
                async with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    async def snapshot(self) -> ConcurrencySnapshot:
        async with self._condition:
            return ConcurrencySnapshot(
                limit=self._limit, active=self._active, waiting=self._waiting
            )
