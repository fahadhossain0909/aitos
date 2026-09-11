"""Simple async token-bucket rate limiter with forensic wait telemetry.

Binance weights each REST endpoint differently and bans/soft-limits IPs
that exceed the per-minute weight budget. The limiter also protects
latency-sensitive callers (such as the live order-book bootstrap) from
being starved by bulk scanner/fallback REST traffic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

from aitos.logging_setup import get_logger

logger = get_logger("aitos.exchange.rate_limiter")


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
        self._acquire_count = 0
        self._wait_count = 0
        self._slow_wait_count = 0
        self._total_wait_seconds = 0.0
        self._max_wait_seconds = 0.0
        self._last_wait_ms = 0.0
        self._last_caller = ""
        self._last_weight = 0
        self._last_critical = False

    def _is_critical_caller(self) -> bool:
        task = asyncio.current_task()
        name = task.get_name() if task is not None else ""
        return any(name.startswith(prefix) for prefix in self._critical_task_prefixes)

    def snapshot(self) -> dict[str, Any]:
        """Return bounded limiter telemetry for forensic health snapshots."""
        return {
            "capacity": self._capacity,
            "reserved_capacity": self._reserved_capacity,
            "refill_per_second": self._refill_per_second,
            "acquire_count": self._acquire_count,
            "wait_count": self._wait_count,
            "slow_wait_count": self._slow_wait_count,
            "total_wait_ms": round(self._total_wait_seconds * 1000, 3),
            "max_wait_ms": round(self._max_wait_seconds * 1000, 3),
            "last_wait_ms": round(self._last_wait_ms, 3),
            "last_caller": self._last_caller,
            "last_weight": self._last_weight,
            "last_critical": self._last_critical,
        }

    async def acquire(self, weight: int = 1) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        if weight > self._capacity:
            raise ValueError(
                f"weight {weight} exceeds bucket capacity {self._capacity}"
            )
        critical = self._is_critical_caller()
        task = asyncio.current_task()
        caller = task.get_name() if task is not None else ""
        started = time.monotonic()
        waited = False
        while True:
            async with self._lock:
                self._refill()
                floor = 0 if critical else self._reserved_capacity
                if self._tokens >= weight + floor:
                    self._tokens -= weight
                    elapsed = time.monotonic() - started
                    self._acquire_count += 1
                    self._last_wait_ms = elapsed * 1000
                    self._last_caller = caller
                    self._last_weight = weight
                    self._last_critical = critical
                    if waited:
                        self._wait_count += 1
                        self._total_wait_seconds += elapsed
                        self._max_wait_seconds = max(self._max_wait_seconds, elapsed)
                        if elapsed >= 0.1:
                            self._slow_wait_count += 1
                            logger.warning(
                                "REST rate-limiter wait",
                                extra={
                                    "aitos_extra": {
                                        "stage": "rate_limiter_wait",
                                        "caller": caller,
                                        "weight": weight,
                                        "critical": critical,
                                        "wait_ms": round(elapsed * 1000, 3),
                                        "tokens_remaining": round(self._tokens, 3),
                                        "reserved_capacity": self._reserved_capacity,
                                    }
                                },
                            )
                    return
                deficit = weight + floor - self._tokens
                wait_seconds = deficit / self._refill_per_second
            waited = True
            await asyncio.sleep(wait_seconds)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._refill_per_second
        )
        self._last_refill = now
