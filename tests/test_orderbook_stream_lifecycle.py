from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook, OrderBookSequenceError
from aitos.models.market import OrderBookSnapshot


def snap(update_id: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 5.0), (99.0, 3.0)),
        asks=((101.0, 4.0), (102.0, 2.0)),
        last_update_id=update_id,
        timestamp=datetime.now(timezone.utc),
    )


def test_stale_update_is_ignored_without_mutating_sequence():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(100))
    book.apply(DepthUpdate(100, 101, 0, ((100.0, 7.0),), (), 1000))
    result = book.apply(DepthUpdate(90, 100, 0, ((100.0, 1.0),), (), 900))
    assert result.last_update_id == 101
    assert result.bids[0] == (100.0, 7.0)


def test_valid_multi_update_chain_replays_in_order():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(200))
    updates = [
        DepthUpdate(199, 201, 0, ((100.0, 6.0),), (), 1000),
        DepthUpdate(202, 204, 201, ((99.0, 4.0),), (), 1100),
        DepthUpdate(205, 205, 204, ((98.0, 2.0),), (), 1200),
    ]
    for update in updates:
        book.apply(update)
    assert book.last_update_id == 205
    assert (98.0, 2.0) in book.snapshot().bids


def test_pu_is_authoritative_even_when_first_update_id_jumps():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(200))
    book.apply(DepthUpdate(199, 201, 0, (), (), 1000))
    # Binance's `pu` links the event to the previously applied event. The
    # first ID may span a range, so continuity must not be rejected solely
    # because U is greater than local+1 when pu is correct.
    result = book.apply(DepthUpdate(203, 205, 201, ((100.0, 6.0),), (), 1100))
    assert result.last_update_id == 205
    assert result.bids[0] == (100.0, 6.0)


def test_gap_requires_resync_before_new_updates_can_be_applied():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(300))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(305, 306, 304, (), (), 1000))
    book.seed(snap(306))
    result = book.apply(DepthUpdate(306, 307, 0, ((100.0, 8.0),), (), 1100))
    assert result.last_update_id == 307
    assert result.bids[0] == (100.0, 8.0)


def test_chain_break_after_reconnect_is_rejected():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(400))
    book.apply(DepthUpdate(400, 401, 0, (), (), 1000))
    with pytest.raises(OrderBookSequenceError):
        book.apply(DepthUpdate(402, 403, 123, (), (), 1100))


def test_reseed_clears_old_levels():
    book = LocalOrderBook("BTCUSDT")
    book.seed(snap(500))
    book.apply(DepthUpdate(500, 501, 0, ((98.0, 9.0),), (), 1000))
    book.seed(snap(600))
    result = book.apply(DepthUpdate(600, 601, 0, (), (), 1100))
    assert result.last_update_id == 601
    assert all(price != 98.0 for price, _ in result.bids)
