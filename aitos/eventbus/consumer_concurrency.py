"""Bounded concurrency helpers for Redis Stream consumers."""

from __future__ import annotations

import os

DEFAULT_CONSUMER_CONCURRENCY = 8
MAX_CONSUMER_CONCURRENCY = 32


def consumer_concurrency() -> int:
    raw = os.getenv("REDIS_CONSUMER_CONCURRENCY")
    if raw is None:
        return DEFAULT_CONSUMER_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONSUMER_CONCURRENCY
    return max(1, min(value, MAX_CONSUMER_CONCURRENCY))
