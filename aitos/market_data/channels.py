"""Semantic channels for the canonical market-data plane.

These names deliberately avoid generation suffixes such as ``v2``/``v3``.
They describe the data contract, not an implementation revision.
"""

from __future__ import annotations

CHANNEL_TRADE = "market.trade"
CHANNEL_BOOK_DELTA = "market.book.delta"
CHANNEL_BOOK_SNAPSHOT = "market.book.snapshot"
CHANNEL_TICKER = "market.ticker"
CHANNEL_KLINE = "market.kline"
CHANNEL_FUNDING = "market.funding"
CHANNEL_OPEN_INTEREST = "market.open_interest"
CHANNEL_LIQUIDATION = "market.liquidation"
CHANNEL_OPTIONS = "market.options"
CHANNEL_INSTRUMENT = "market.instrument"

ALL_CHANNELS = (
    CHANNEL_TRADE,
    CHANNEL_BOOK_DELTA,
    CHANNEL_BOOK_SNAPSHOT,
    CHANNEL_TICKER,
    CHANNEL_KLINE,
    CHANNEL_FUNDING,
    CHANNEL_OPEN_INTEREST,
    CHANNEL_LIQUIDATION,
    CHANNEL_OPTIONS,
    CHANNEL_INSTRUMENT,
)

# Purpose-specific consumer names. A consumer group is stable across restarts;
# the application must create it idempotently rather than repeatedly issuing
# XGROUP CREATE on every processing loop.
GROUP_SCANNER = "market-scanner"
GROUP_PERSISTENCE = "market-persistence"
GROUP_FEATURES = "market-features"
