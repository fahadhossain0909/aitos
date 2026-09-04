"""Small, deterministic reliability primitives used by the trading runtime.

These primitives intentionally have no network/database dependencies. They are
safe to unit-test and can be composed around Redis, database and exchange
operations without coupling the domain layer to a particular vendor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class RetryExhaustedError(RuntimeError):
    """Raised when an operation still fails after all retry attempts."""


class CircuitOpenError(RuntimeError):
    """Raised before an operation when its circuit is open."""


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    attempts: int = 5
    initial_delay: float = 0.25
    multiplier: float = 2.0
    max_delay: float = 10.0
    jitter: float = 0.10

    def delay(self, retry_number: int) -> float:
        base = min(self.max_delay, self.initial_delay * (self.multiplier**retry_number))
        # Deterministic jitter is preferable for tests; runtime callers can
        # supply their own sleep policy if non-deterministic jitter is required.
        return max(0.0, base * (1.0 + self.jitter))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: BackoffPolicy | None = None,
    retryable: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run an async operation with bounded exponential-backoff retries."""
    p = policy or BackoffPolicy()
    if p.attempts < 1:
        raise ValueError("BackoffPolicy.attempts must be >= 1")
    last: Exception | None = None
    for attempt in range(p.attempts):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if retryable is not None and not retryable(exc):
                raise
            if attempt + 1 >= p.attempts:
                break
            await sleep(p.delay(attempt))
    raise RetryExhaustedError(f"operation failed after {p.attempts} attempts") from last


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Failure-count circuit breaker with cooldown and half-open probing."""

    def __init__(
        self, failure_threshold: int = 5, recovery_timeout: float = 30.0
    ) -> None:
        if failure_threshold < 1 or recovery_timeout <= 0:
            raise ValueError("invalid circuit-breaker configuration")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.failures = 0
        self._opened_at: float | None = None

    def allow(self, now: float | None = None) -> bool:
        if self.state is CircuitState.CLOSED:
            return True
        if self.state is CircuitState.OPEN:
            current = time.monotonic() if now is None else now
            if (
                self._opened_at is not None
                and current - self._opened_at >= self.recovery_timeout
            ):
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self.state = CircuitState.CLOSED
        self._opened_at = None

    def failure(self, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self._opened_at = time.monotonic() if now is None else now

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        if not self.allow():
            raise CircuitOpenError("circuit breaker is open")
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure()
            raise
        else:
            self.success()
            return result


class Bulkhead:
    """Bounded concurrency gate; excess work waits instead of spawning tasks."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("Bulkhead capacity must be >= 1")
        self.capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            return await operation()


@dataclass(frozen=True, slots=True)
class JournalRecord:
    sequence: int
    record_id: str
    kind: str
    created_at: str
    payload: Mapping[str, Any]
    previous_hash: str
    record_hash: str


class AppendOnlyJournal:
    """Hash-chained JSONL journal suitable for deterministic state replay.

    Each record includes the hash of the previous record. This is not intended
    as cryptographic proof of external identity; it is an inexpensive tamper-
    evident chain and a stable replay source for process recovery.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self, kind: str, payload: Mapping[str, Any], created_at: str
    ) -> JournalRecord:
        previous = self._last_hash()
        body = {
            "sequence": self._next_sequence(),
            "record_id": str(uuid.uuid4()),
            "kind": kind,
            "created_at": created_at,
            "payload": dict(payload),
            "previous_hash": previous,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        body["record_hash"] = record_hash
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
        return JournalRecord(**body)

    def replay(self) -> list[JournalRecord]:
        records: list[JournalRecord] = []
        previous = ""
        if not self.path.exists():
            return records
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            supplied = raw.pop("record_hash")
            canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if supplied != expected or raw["previous_hash"] != previous:
                raise ValueError("journal integrity check failed")
            raw["record_hash"] = supplied
            records.append(JournalRecord(**raw))
            previous = supplied
        return records

    def _last_hash(self) -> str:
        records = self.replay()
        return records[-1].record_hash if records else ""

    def _next_sequence(self) -> int:
        records = self.replay()
        return records[-1].sequence + 1 if records else 1
