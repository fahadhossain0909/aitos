"""Safety guard for stale REST trade fallbacks.

REST is used only as a recovery/fallback source. A response containing only
historical trades must never silently become strategy input. This wrapper
filters by source timestamp while preserving trade order and IDs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.logging_setup import get_logger

logger = get_logger("aitos.exchange.rest_trade_guard")

DEFAULT_MAX_REST_TRADE_AGE_SECONDS = 15.0


def _age_seconds(timestamp: datetime, now: datetime) -> float:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - timestamp.astimezone(timezone.utc)).total_seconds())


def filter_fresh_trades(
    trades: list[Any],
    *,
    max_age_seconds: float = DEFAULT_MAX_REST_TRADE_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[list[Any], int]:
    """Return fresh REST trades without reordering or mutating them."""
    reference = now or datetime.now(timezone.utc)
    accepted: list[Any] = []
    rejected = 0
    for trade in trades:
        timestamp = getattr(trade, "timestamp", None)
        if timestamp is None or _age_seconds(timestamp, reference) <= max_age_seconds:
            accepted.append(trade)
        else:
            rejected += 1
    return accepted, rejected


def install_rest_trade_guard(adapter_cls: type[Any]) -> None:
    """Guard ExchangeAdapter REST trade reads at the source boundary."""
    if getattr(adapter_cls, "_rest_trade_guard_installed", False):
        return
    adapter_cls._rest_trade_guard_installed = True
    original = adapter_cls.fetch_recent_trades

    @wraps(original)
    async def guarded(self: Any, symbol: str, limit: int = 500) -> list[Any]:
        trades = await original(self, symbol, limit=limit)
        fresh, rejected = filter_fresh_trades(trades)
        if rejected:
            logger.warning(
                "stale REST trade response filtered at exchange boundary",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "received_count": len(trades),
                        "accepted_count": len(fresh),
                        "rejected_count": rejected,
                        "max_age_seconds": DEFAULT_MAX_REST_TRADE_AGE_SECONDS,
                    }
                },
            )
        return fresh

    adapter_cls.fetch_recent_trades = guarded


__all__ = [
    "DEFAULT_MAX_REST_TRADE_AGE_SECONDS",
    "filter_fresh_trades",
    "install_rest_trade_guard",
]
