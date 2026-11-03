from datetime import datetime, timezone

from aitos.data.orderbook_replay import OrderBookReconstructor
from aitos.data.schema import CanonicalBookEvent


def event(update_id, side, price, quantity):
    return CanonicalBookEvent(
        "binance",
        "futures_um",
        "BTCUSDT",
        update_id,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        side,
        price,
        quantity,
    )


def test_snapshot_and_absolute_updates():
    book = OrderBookReconstructor()
    book.load_snapshot([(100, 2), (99, 3)], [(101, 4)], 10)
    assert book.best_bid() == (100.0, 2.0)
    assert book.best_ask() == (101.0, 4.0)

    assert book.apply(event(11, "buy", 100, 5))
    assert book.best_bid() == (100.0, 5.0)

    assert book.apply(event(12, "sell", 101, 0))
    assert book.best_ask() is None


def test_stale_update_is_rejected():
    book = OrderBookReconstructor()
    book.load_snapshot([(100, 2)], [], 20)
    assert not book.apply(event(19, "buy", 100, 9))
    assert book.best_bid() == (100.0, 2.0)
