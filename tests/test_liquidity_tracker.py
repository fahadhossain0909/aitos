from datetime import datetime, timezone

from aitos.intelligence.liquidity_tracker import LiquidityTracker
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


def book(bids, asks, update_id):
    return OrderBookSnapshot(
        "BTCUSDT", tuple(bids), tuple(asks), update_id, datetime.now(timezone.utc)
    )


def trade(qty, side, maker=False):
    return TradeTick("BTCUSDT", 1, 100.0, qty, side, maker, datetime.now(timezone.utc))


def test_detects_stacking_and_pulling():
    tracker = LiquidityTracker(removal_ratio=0.25)
    tracker.update(book([(99, 10)], [(101, 10)], 1))
    events = tracker.update(book([(99, 20)], [(101, 5)], 2))
    kinds = {(e.kind, e.side) for e in events}
    assert ("stacking", "bid") in kinds
    assert ("pulling", "ask") in kinds


def test_detects_buy_side_sweep():
    tracker = LiquidityTracker(removal_ratio=0.25)
    tracker.update(book([(99, 10)], [(101, 20), (102, 10)], 1))
    events = tracker.update(
        book([(99, 10)], [(101, 5), (102, 2)], 2), [trade(15, TradeSide.BUY)]
    )
    assert any(e.kind == "sweep" and e.side == "ask" for e in events)


def test_detects_sell_side_sweep():
    tracker = LiquidityTracker(removal_ratio=0.25)
    tracker.update(book([(99, 20), (98, 10)], [(101, 10)], 1))
    events = tracker.update(
        book([(99, 5), (98, 2)], [(101, 10)], 2),
        [trade(15, TradeSide.SELL, maker=True)],
    )
    assert any(e.kind == "sweep" and e.side == "bid" for e in events)


def test_pressure_is_bounded():
    tracker = LiquidityTracker()
    score = tracker.pressure_score(book([(99, 100)], [(101, 10)], 1))
    assert 0.0 <= score <= 10.0
