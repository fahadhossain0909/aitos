from datetime import datetime, timedelta, timezone

from aitos.intelligence.amt import (AMTEngine, AuctionState, DayType,
                                    ValueMigration, build_volume_profile)
from aitos.models.market import TradeSide, TradeTick


def trades(prices, quantities=None, start=None):
    quantities = quantities or [1.0] * len(prices)
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        TradeTick(
            "BTCUSDT",
            i,
            float(price),
            float(qty),
            TradeSide.BUY,
            False,
            start + timedelta(seconds=i),
        )
        for i, (price, qty) in enumerate(zip(prices, quantities))
    ]


def test_volume_profile_finds_poc_and_value_area():
    profile = build_volume_profile(
        trades([100, 101, 101, 102, 102, 102, 103], [1, 1, 2, 1, 2, 3, 1]), 1.0
    )
    assert profile.poc == 102.0
    assert profile.val <= profile.poc <= profile.vah
    assert profile.value_area_pct >= 0.70


def test_amt_engine_returns_structured_context():
    context = AMTEngine(1.0).analyze(
        trades([100, 101, 101, 102, 102, 102, 103, 102, 101])
    )
    assert context.poc == 102.0
    assert context.vah >= context.poc >= context.val
    assert 0.0 <= context.acceptance <= 1.0
    assert 0.0 <= context.rejection <= 1.0
    assert 0.0 <= context.confidence <= 1.0
    assert 0.0 <= context.data_quality <= 1.0
    assert context.state in set(AuctionState)
    assert context.open_location != "unknown"


def test_initial_balance_and_extensions_are_session_aware():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [
        start,
        start + timedelta(minutes=30),
        start + timedelta(minutes=45),
        start + timedelta(minutes=90),
        start + timedelta(minutes=91),
        start + timedelta(minutes=92),
    ]
    ticks = [
        TradeTick("BTCUSDT", i, price, 1.0, TradeSide.BUY, False, ts)
        for i, (price, ts) in enumerate(zip([100, 102, 101, 105, 107, 106], timestamps))
    ]
    context = AMTEngine(1.0, ib_minutes=60).analyze(ticks, session_start=start)
    assert context.ib_high == 102.0
    assert context.ib_low == 100.0
    assert context.ib_range == 2.0
    assert context.ib_extension_up > 0.0
    assert context.open_price == 100.0


def test_value_migration_uses_previous_poc():
    old = build_volume_profile(trades([100, 100, 101, 101]), 1.0)
    new = build_volume_profile(trades([103, 103, 104, 104, 104]), 1.0)
    context = AMTEngine(1.0).analyze(
        trades([103, 103, 104, 104, 104]), previous_profile=old
    )
    assert new.poc > old.poc
    assert context.value_migration == ValueMigration.UP


def test_double_distribution_detection():
    prices = [100, 100, 101, 102, 103, 104, 105, 105, 104, 103, 102, 101, 100]
    quantities = [5, 5, 4, 1, 1, 1, 1, 5, 4, 1, 1, 4, 5]
    context = AMTEngine(1.0).analyze(trades(prices, quantities))
    assert context.day_type in {
        DayType.DOUBLE_DISTRIBUTION,
        DayType.NEUTRAL,
        DayType.NORMAL,
    }
