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
    # After seed, _awaiting_first_update is False; first update must have
    # previous_update_id == last_update_id to be accepted.
    result = book.apply(DepthUpdate(101, 102, 100, ((100.0, 7.0),), ((101.0, 0.0),), 1000))
    assert result.last_update_id == 102
    assert result.bids[0] == (100.0, 7.0)
    assert result.asks[0] == (102.0, 2.0)


def test_subsequent_update_requires_pu_continuity():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    book.apply(DepthUpdate(101, 102, 100, (), (), 1000))
    book.apply(DepthUpdate(103, 103, 102, ((99.0, 0.0),), (), 1100))
    assert book.last_update_id == 103


def test_gap_is_rejected():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(105, 106, 104, (), (), 1000))


def test_chain_break_is_rejected():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    book.apply(DepthUpdate(101, 102, 100, (), (), 1000))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(103, 104, 999, (), (), 1100))


def test_zero_quantity_removes_price_level():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snapshot(100))
    result = book.apply(DepthUpdate(101, 102, 100, ((100.0, 0.0),), (), 1000))
    assert all(price != 100.0 for price, _ in result.bids)
