"""Differential replay checks against AITOS' existing Python LocalOrderBook."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitos.exchange.orderbook import (
    DepthUpdate,
    LocalOrderBook,
    OrderBookSequenceError,
)
from aitos.models.market import OrderBookSnapshot
from aitos.native_core import (
    InstrumentScale,
    NativeOrderBookSession,
    native_available,
)


def _snapshot():
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.00, 1.250), (99.00, 2.000)),
        asks=((101.00, 1.500), (102.00, 3.000)),
        last_update_id=10,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("prefer_native", [False, True])
def test_native_session_replay_matches_existing_python_book(prefer_native):
    if prefer_native and not native_available():
        pytest.skip("optional Rust extension is not installed")

    initial = _snapshot()
    scale = InstrumentScale.from_exchange_filters(
        SimpleNamespace(tick_size=0.01, step_size=0.001)
    )
    native = NativeOrderBookSession(
        scale,
        initial.last_update_id,
        initial.bids,
        initial.asks,
        prefer_native=prefer_native,
    )
    python = LocalOrderBook("BTCUSDT", max_levels=20)
    python.seed(initial)

    updates = [
        DepthUpdate(11, 11, 10, ((100.00, 0.750),), (), 1_000),
        DepthUpdate(
            12,
            12,
            11,
            ((99.00, 0.0),),
            ((101.00, 0.0), (101.50, 0.875)),
            2_000,
        ),
        DepthUpdate(13, 13, 12, (), ((102.00, 1.250),), 3_000),
    ]

    for update in updates:
        expected = python.apply(update)
        assert expected is not None
        assert native.apply_depth_update(update)
        update_id, bids, asks = native.snapshot(20)
        assert update_id == expected.last_update_id
        assert [(float(price), float(qty)) for price, qty in bids] == list(
            expected.bids
        )
        assert [(float(price), float(qty)) for price, qty in asks] == list(
            expected.asks
        )


def test_native_session_rejects_sequence_gap_without_mutation():
    scale = InstrumentScale.from_exchange_filters(
        SimpleNamespace(tick_size=0.01, step_size=0.001)
    )
    session = NativeOrderBookSession(
        scale,
        10,
        [("100.00", "1.000")],
        [("101.00", "1.000")],
        prefer_native=False,
    )
    with pytest.raises(ValueError, match="bridge"):
        session.apply_depth_update(DepthUpdate(12, 13, 11, (), (), 1_000))
    assert session.snapshot(5) == (
        10,
        [(scale.price_from_ticks(10_000), scale.quantity_from_lots(1_000))],
        [(scale.price_from_ticks(10_100), scale.quantity_from_lots(1_000))],
    )


def test_existing_python_book_still_fails_closed_on_gap():
    python = LocalOrderBook("BTCUSDT", max_levels=20)
    python.seed(_snapshot())
    with pytest.raises(OrderBookSequenceError):
        python.apply(DepthUpdate(12, 13, 11, (), (), 1_000))
