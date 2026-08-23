"""Retry-with-backoff — the missing production-supervision piece flagged
in earlier phases' READMEs. Used to wrap infra connection attempts
(Redis, ClickHouse, Neo4j) so a transient outage at startup or during a
brief reconnect doesn't take the whole process down.

Deliberately small and dependency-free — no third-party retry library,
since exponential backoff with jitter is about a dozen lines and pulling
in a dependency for it would be disproportionate.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Tuple, TypeVar

from aitos.logging_setup import get_logger

logger = get_logger("aitos.resilience")

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when every retry attempt failed. Wraps the last underlying
    exception so the original cause isn't lost."""

    def __init__(self, attempts: int, last_exception: BaseException) -> None:
        super().__init__(f"gave up after {attempts} attempts: {last_exception}")
        self.attempts = attempts
        self.last_exception = last_exception


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    max_attempts: int = 5,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 30.0,
    exceptions: Tuple[type, ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """Call ``fn()`` (a zero-arg async callable — use a lambda/partial to
    bind arguments), retrying on any of ``exceptions`` with exponential
    backoff plus jitter (avoids a thundering-herd reconnect if multiple
    instances restart at once). Raises ``RetryExhaustedError`` if every
    attempt fails; the immediate exception from a non-matching exception
    type still propagates unchanged (only ``exceptions`` are retried).
    """
    last_exception: BaseException = RuntimeError("unreachable")
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except exceptions as exc:  # noqa: BLE001
            last_exception = exc
            if attempt == max_attempts:
                break
            delay = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
            jitter = random.uniform(0, delay * 0.25)
            wait_time = delay + jitter
            logger.warning(
                f"{operation_name} failed (attempt {attempt}/{max_attempts}), retrying in {wait_time:.1f}s",
                extra={
                    "aitos_extra": {
                        "operation": operation_name,
                        "attempt": attempt,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.sleep(wait_time)

    raise RetryExhaustedError(max_attempts, last_exception)
