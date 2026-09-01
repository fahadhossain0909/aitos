"""Safety helpers for validating REST trade recovery windows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from aitos.models.market import TradeTick

DEFAULT_MAX_SOURCE_AGE_SECONDS = 15.0


def source_age_seconds(trade: TradeTick, now: datetime | None = None) -> float:
    """Return non-negative source age in seconds."""
    reference = now or datetime.now(timezone.utc)
    timestamp = trade.timestamp.astimezone(timezone.utc)
    return max(0.0, (reference - timestamp).total_seconds())


def validate_recovery_window(
    trades: Iterable[TradeTick],
    *,
    previous_trade_id: int = -1,
    previous_source_timestamp: datetime | None = None,
    max_source_age_seconds: float = DEFAULT_MAX_SOURCE_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[list[TradeTick], int]:
    """Validate a REST recovery window without advancing state on stale data.

    Returns accepted trades and the number rejected for freshness/order violations.
    A recovery batch is accepted only when trade IDs and source timestamps are
    monotonically increasing. This prevents a REST response containing newer
    IDs but historical timestamps from masquerading as a live recovery stream.
    """
    ordered = sorted(trades, key=lambda trade: trade.trade_id)
    accepted: list[TradeTick] = []
    rejected = 0
    last_id = previous_trade_id
    last_timestamp = previous_source_timestamp

    for trade in ordered:
        timestamp = trade.timestamp.astimezone(timezone.utc)
        if trade.trade_id <= last_id:
            rejected += 1
            continue
        if last_timestamp is not None and timestamp < last_timestamp:
            rejected += 1
            continue
        if source_age_seconds(trade, now) > max_source_age_seconds:
            rejected += 1
            continue
        accepted.append(trade)
        last_id = trade.trade_id
        last_timestamp = timestamp

    return accepted, rejected
