from datetime import datetime, timedelta, timezone

from aitos.data.trade_recovery import validate_recovery_window
from aitos.models.market import TradeSide, TradeTick


def trade(trade_id: int, age_seconds: float, *, base: datetime) -> TradeTick:
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=100.0,
        quantity=1.0,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=base - timedelta(seconds=age_seconds),
    )


def test_rejects_historical_recovery_with_newer_ids() -> None:
    now = datetime.now(timezone.utc)
    batch = [trade(101, 16.0, base=now), trade(102, 17.0, base=now)]
    accepted, rejected = validate_recovery_window(batch, now=now)
    assert accepted == []
    assert rejected == 2


def test_rejects_timestamp_regression_even_when_ids_increase() -> None:
    now = datetime.now(timezone.utc)
    batch = [trade(101, 2.0, base=now), trade(102, 4.0, base=now)]
    accepted, rejected = validate_recovery_window(batch, now=now)
    assert [item.trade_id for item in accepted] == [101]
    assert rejected == 1


def test_accepts_fresh_monotonic_recovery() -> None:
    now = datetime.now(timezone.utc)
    batch = [trade(101, 4.0, base=now), trade(102, 3.0, base=now)]
    accepted, rejected = validate_recovery_window(batch, now=now)
    assert [item.trade_id for item in accepted] == [101, 102]
    assert rejected == 0


def test_previous_id_and_timestamp_are_respected() -> None:
    now = datetime.now(timezone.utc)
    batch = [
        trade(100, 2.0, base=now),
        trade(101, 1.0, base=now),
        trade(102, 0.5, base=now),
    ]
    previous_timestamp = now - timedelta(seconds=3)
    accepted, rejected = validate_recovery_window(
        batch,
        previous_trade_id=100,
        previous_source_timestamp=previous_timestamp,
        now=now,
    )
    assert [item.trade_id for item in accepted] == [101, 102]
    assert rejected == 1
