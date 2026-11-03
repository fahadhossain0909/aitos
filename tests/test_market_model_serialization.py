from datetime import datetime, timezone

from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


def test_trade_tick_round_trip():
    tick = TradeTick(
        "BTCUSDT", 123, 100.5, 0.25, TradeSide.BUY, False, datetime.now(timezone.utc)
    )
    assert TradeTick.from_dict(tick.to_dict()) == tick


def test_order_book_round_trip():
    book = OrderBookSnapshot(
        "BTCUSDT",
        ((100.0, 2.0), (99.5, 1.0)),
        ((100.5, 1.5), (101.0, 3.0)),
        12345,
        datetime.now(timezone.utc),
    )
    restored = OrderBookSnapshot.from_dict(book.to_dict())
    assert restored == book
    assert restored.best_bid == 100.0
    assert restored.best_ask == 100.5
