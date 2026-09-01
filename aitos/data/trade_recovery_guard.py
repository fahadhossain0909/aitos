"""Monotonicity guard for REST trade recovery batches."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.logging_setup import get_logger

logger = get_logger("aitos.data.trade_recovery_guard")

DEFAULT_MAX_SOURCE_AGE_SECONDS = 15.0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_recovery_window(
    trades: Iterable[Any],
    *,
    previous_trade_id: int = -1,
    previous_source_timestamp: datetime | None = None,
    max_source_age_seconds: float = DEFAULT_MAX_SOURCE_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[list[Any], int]:
    """Accept only fresh, ID- and timestamp-monotonic recovery trades."""
    reference = _utc(now or datetime.now(timezone.utc))
    ordered = sorted(trades, key=lambda trade: int(trade.trade_id))
    accepted: list[Any] = []
    rejected = 0
    last_id = previous_trade_id
    last_timestamp = (
        _utc(previous_source_timestamp)
        if previous_source_timestamp is not None
        else None
    )

    for trade in ordered:
        trade_id = int(trade.trade_id)
        timestamp = _utc(trade.timestamp)
        if trade_id <= last_id:
            rejected += 1
            continue
        if last_timestamp is not None and timestamp < last_timestamp:
            rejected += 1
            continue
        if (reference - timestamp).total_seconds() > max_source_age_seconds:
            rejected += 1
            continue
        accepted.append(trade)
        last_id = trade_id
        last_timestamp = timestamp

    return accepted, rejected


def install_trade_recovery_guard(service_cls: type[Any]) -> None:
    """Wrap ingestion recovery with ID/timestamp monotonicity validation."""
    if getattr(service_cls, "_trade_recovery_guard_installed", False):
        return
    service_cls._trade_recovery_guard_installed = True
    original_process = service_cls._process_trade_batch

    @wraps(original_process)
    async def guarded_process(self: Any, trades: list[Any]) -> None:
        if not getattr(self, "_transport_rest_recovery_active", False):
            await original_process(self, trades)
            return

        symbol_groups: dict[str, list[Any]] = {}
        for trade in trades:
            symbol_groups.setdefault(str(trade.symbol).upper(), []).append(trade)

        accepted: list[Any] = []
        rejected_total = 0
        for symbol, group in symbol_groups.items():
            previous_id = getattr(self, "_last_trade_ids", {}).get(symbol, -1)
            previous_ts = getattr(self, "_trade_recovery_source_timestamps", {}).get(
                symbol
            )
            valid, rejected = validate_recovery_window(
                group,
                previous_trade_id=previous_id,
                previous_source_timestamp=previous_ts,
            )
            accepted.extend(valid)
            rejected_total += rejected

        if rejected_total:
            self._trade_recovery_guard_rejected = (
                getattr(self, "_trade_recovery_guard_rejected", 0) + rejected_total
            )
            if hasattr(self, "_transport_rest_stale_trades_filtered"):
                self._transport_rest_stale_trades_filtered += rejected_total
            logger.warning(
                "REST recovery trades rejected by monotonicity guard",
                extra={"aitos_extra": {"rejected": rejected_total}},
            )

        if not accepted:
            return

        await original_process(self, accepted)
        source_timestamps = getattr(self, "_trade_recovery_source_timestamps", None)
        if source_timestamps is None:
            source_timestamps = {}
            self._trade_recovery_source_timestamps = source_timestamps
        for trade in accepted:
            source_timestamps[str(trade.symbol).upper()] = _utc(trade.timestamp)

    service_cls._process_trade_batch = guarded_process

    original_init = service_cls.__init__

    @wraps(original_init)
    def init_wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._trade_recovery_source_timestamps = {}
        self._trade_recovery_guard_rejected = 0

    service_cls.__init__ = init_wrapper
