"""Simple async token-bucket rate limiter with a protected critical lane.

Binance weights each REST endpoint differently and bans/soft-limits IPs
that exceed the per-minute weight budget. The limiter also protects
latency-sensitive callers (such as the live order-book bootstrap) from
being starved by bulk scanner/fallback REST traffic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable


class TokenBucketRateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        reserved_capacity: int = 100,
        critical_task_prefixes: Iterable[str] = ("market-data-orderbook",),
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        if reserved_capacity < 0 or reserved_capacity >= capacity:
            raise ValueError("reserved_capacity must be >= 0 and < capacity")
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_per_second = refill_per_second
        self._reserved_capacity = reserved_capacity
        self._critical_task_prefixes = tuple(critical_task_prefixes)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _is_critical_caller(self) -> bool:
        task = asyncio.current_task()
        name = task.get_name() if task is not None else ""
        return any(name.startswith(prefix) for prefix in self._critical_task_prefixes)

    async def acquire(self, weight: int = 1) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        if weight > self._capacity:
            raise ValueError(
                f"weight {weight} exceeds bucket capacity {self._capacity}"
            )
        critical = self._is_critical_caller()
        while True:
            async with self._lock:
                self._refill()
                floor = 0 if critical else self._reserved_capacity
                if self._tokens >= weight + floor:
                    self._tokens -= weight
                    return
                deficit = weight + floor - self._tokens
                wait_seconds = deficit / self._refill_per_second
            await asyncio.sleep(wait_seconds)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._refill_per_second
        )
        self._last_refill = now
