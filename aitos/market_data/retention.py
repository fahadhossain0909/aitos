"""Retention policy for the canonical Redis market-data channels."""

from __future__ import annotations

from .channels import (
    CHANNEL_BOOK_DELTA,
    CHANNEL_BOOK_SNAPSHOT,
    CHANNEL_FUNDING,
    CHANNEL_INSTRUMENT,
    CHANNEL_KLINE,
    CHANNEL_LIQUIDATION,
    CHANNEL_OPEN_INTEREST,
    CHANNEL_OPTIONS,
    CHANNEL_TICKER,
    CHANNEL_TRADE,
)

# Redis is a transport/hot-state layer. Durable history belongs in ClickHouse.
STREAM_MAXLEN: dict[str, int] = {
    CHANNEL_TRADE: 25_000,
    CHANNEL_BOOK_DELTA: 25_000,
    CHANNEL_BOOK_SNAPSHOT: 5_000,
    CHANNEL_TICKER: 10_000,
    CHANNEL_KLINE: 10_000,
    CHANNEL_FUNDING: 5_000,
    CHANNEL_OPEN_INTEREST: 10_000,
    CHANNEL_LIQUIDATION: 25_000,
    CHANNEL_OPTIONS: 10_000,
    CHANNEL_INSTRUMENT: 2_000,
}


def maxlen_for(channel: str) -> int | None:
    return STREAM_MAXLEN.get(channel)
