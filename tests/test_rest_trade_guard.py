from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.exchange.rest_trade_guard import filter_fresh_trades


def test_filter_preserves_order_and_rejects_only_stale_trades() -> None:
    now = datetime.now(timezone.utc)
    trades = [
        SimpleNamespace(trade_id=10, timestamp=now - timedelta(seconds=1)),
        SimpleNamespace(trade_id=11, timestamp=now - timedelta(seconds=30)),
        SimpleNamespace(trade_id=12, timestamp=now - timedelta(seconds=2)),
    ]

    fresh, rejected = filter_fresh_trades(trades, max_age_seconds=15, now=now)

    assert [trade.trade_id for trade in fresh] == [10, 12]
    assert rejected == 1


def test_filter_does_not_reorder_fresh_trades() -> None:
    now = datetime.now(timezone.utc)
    trades = [
        SimpleNamespace(trade_id=20, timestamp=now - timedelta(seconds=3)),
        SimpleNamespace(trade_id=18, timestamp=now - timedelta(seconds=2)),
    ]

    fresh, rejected = filter_fresh_trades(trades, max_age_seconds=15, now=now)

    assert [trade.trade_id for trade in fresh] == [20, 18]
    assert rejected == 0


def test_missing_timestamp_is_not_silently_dropped() -> None:
    now = datetime.now(timezone.utc)
    trade = SimpleNamespace(trade_id=30, timestamp=None)

    fresh, rejected = filter_fresh_trades([trade], max_age_seconds=15, now=now)

    assert fresh == [trade]
    assert rejected == 0
