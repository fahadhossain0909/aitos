"""Simple async token-bucket rate limiter.

Binance weights each REST endpoint differently and bans/soft-limits IPs
that exceed the per-minute weight budget. This limiter lets the adapter
declare a weight per call and await capacity before firing the request,
rather than relying on try/except around 429/418 responses.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_per_second = refill_per_second
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        if weight > self._capacity:
            raise ValueError(
                f"weight {weight} exceeds bucket capacity {self._capacity}"
            )
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                deficit = weight - self._tokens
                wait_seconds = deficit / self._refill_per_second
            await asyncio.sleep(wait_seconds)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._refill_per_second
        )
        self._last_refill = now
