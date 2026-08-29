from datetime import datetime, timezone

import pytest

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook, OrderBookSequenceError
from aitos.models.market import OrderBookSnapshot


def snapshot(update_id: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 5.0), (99.0, 3.0)),
        asks=((101.0, 4.0), (102.0, 2.0)),
        last_update_id=update_id,
        timestamp=datetime.now(timezone.utc),
    )


def test_first_diff_bridges_rest_snapshot():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    result = book.apply(DepthUpdate(99, 101, 0, ((100.0, 7.0),), ((101.0, 0.0),), 1000))
    assert result.last_update_id == 101
    assert result.bids[0] == (100.0, 7.0)
    assert result.asks[0] == (102.0, 2.0)


def test_subsequent_update_requires_pu_continuity():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    book.apply(DepthUpdate(100, 101, 0, (), (), 1000))
    book.apply(DepthUpdate(102, 102, 101, ((99.0, 0.0),), (), 1100))
    assert book.last_update_id == 102


def test_gap_is_rejected():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(105, 106, 104, (), (), 1000))


def test_chain_break_is_rejected():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    book.apply(DepthUpdate(100, 101, 0, (), (), 1000))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(102, 103, 999, (), (), 1100))


def test_zero_quantity_removes_price_level():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    result = book.apply(DepthUpdate(100, 101, 0, ((100.0, 0.0),), (), 1000))
    assert all(price != 100.0 for price, _ in result.bids)
