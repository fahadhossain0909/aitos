"""Reliability primitives for autonomous trading runtime.

The package is dependency-light and deliberately keeps infrastructure concerns
outside strategy logic: bounded concurrency, retry/backoff, circuit breaking,
and append-only state/event journaling are reusable by adapters and services.
"""

from .primitives import (
    AppendOnlyJournal,
    BackoffPolicy,
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    JournalRecord,
    RetryExhaustedError,
    retry_async,
)

__all__ = [
    "AppendOnlyJournal",
    "BackoffPolicy",
    "Bulkhead",
    "CircuitBreaker",
    "CircuitOpenError",
    "JournalRecord",
    "RetryExhaustedError",
    "retry_async",
]
