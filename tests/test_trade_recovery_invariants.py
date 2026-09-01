from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradePoint:
    trade_id: int
    source_ts: float


def validate_recovery_batch(
    batch: list[TradePoint],
    last_trade_id: int | None,
    last_source_ts: float | None,
    max_source_age: float,
    now: float,
) -> list[TradePoint]:
    accepted: list[TradePoint] = []
    previous_id = last_trade_id
    previous_ts = last_source_ts
    for trade in batch:
        if now - trade.source_ts > max_source_age:
            continue
        if previous_id is not None and trade.trade_id <= previous_id:
            continue
        if previous_ts is not None and trade.source_ts < previous_ts:
            continue
        accepted.append(trade)
        previous_id = trade.trade_id
        previous_ts = trade.source_ts
    return accepted


def test_new_id_with_old_timestamp_is_rejected() -> None:
    accepted = validate_recovery_batch(
        [TradePoint(101, 100.0)], 100, 120.0, 15.0, 125.0
    )
    assert accepted == []


def test_monotonic_fresh_batch_is_accepted() -> None:
    batch = [TradePoint(101, 121.0), TradePoint(102, 122.0)]
    assert validate_recovery_batch(batch, 100, 120.0, 15.0, 125.0) == batch


def test_stale_batch_cannot_advance_watermark() -> None:
    accepted = validate_recovery_batch(
        [TradePoint(101, 90.0), TradePoint(102, 91.0)],
        100,
        120.0,
        15.0,
        125.0,
    )
    assert accepted == []


def test_id_and_timestamp_regression_is_rejected() -> None:
    accepted = validate_recovery_batch(
        [TradePoint(101, 121.0), TradePoint(100, 122.0)],
        100,
        120.0,
        15.0,
        125.0,
    )
    assert accepted == [TradePoint(101, 121.0)]
