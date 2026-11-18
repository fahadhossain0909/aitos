"""Contract tests for the optional native core and Python reference fallback."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aitos.native_core import (
    InstrumentScale,
    NativeOrderBookSession,
    PythonFixedPointOrderBook,
    create_order_book,
    native_available,
)


def exercise(book):
    assert book.apply_delta(10, 11, bids=[(102, 2)])
    assert book.apply_delta(
        12,
        12,
        bids=[(101, 0)],
        asks=[(103, 0), (104, 7)],
        previous_update_id=11,
    )
    return book.snapshot(5), book.best_bid_ask()


def test_reference_book_and_best_prices():
    book = PythonFixedPointOrderBook(10, [(100, 5), (101, 3)], [(103, 4)])
    assert exercise(book) == (
        (12, [(102, 2), (100, 5)], [(104, 7)]),
        (102, 104),
    )


def test_stale_delta_is_ignored():
    book = PythonFixedPointOrderBook(10, [(100, 1)], [(102, 1)])
    assert not book.apply_delta(1, 10, bids=[(99, 5)])
    assert book.snapshot(5) == (10, [(100, 1)], [(102, 1)])


def test_gap_and_invalid_level_fail_without_mutation():
    book = PythonFixedPointOrderBook(10, [(100, 1)], [(102, 1)])
    with pytest.raises(ValueError):
        book.apply_delta(12, 13, bids=[(101, 9)])
    with pytest.raises(ValueError):
        book.apply_delta(11, 11, bids=[(101, -1)])
    assert book.snapshot(5) == (10, [(100, 1)], [(102, 1)])


def test_factory_can_force_reference():
    assert isinstance(
        create_order_book(10, prefer_native=False), PythonFixedPointOrderBook
    )


def test_decimal_tick_and_lot_conversion_is_exact():
    scale = InstrumentScale(Decimal("0.01"), Decimal("0.001"))
    assert scale.price_to_ticks("65000.12") == 6_500_012
    assert scale.quantity_to_lots("0.125") == 125
    assert scale.price_from_ticks(6_500_012) == Decimal("65000.12")
    assert scale.quantity_from_lots(125) == Decimal("0.125")
    with pytest.raises(ValueError, match="not aligned"):
        scale.price_to_ticks("65000.123")
    with pytest.raises(ValueError, match="not aligned"):
        scale.quantity_to_lots("0.0005")


def test_scale_rejects_non_finite_and_invalid_values():
    with pytest.raises(ValueError):
        InstrumentScale(Decimal("0"), Decimal("0.001"))
    scale = InstrumentScale(Decimal("0.01"), Decimal("0.001"))
    with pytest.raises(ValueError):
        scale.price_to_ticks("NaN")
    with pytest.raises(ValueError):
        scale.quantity_to_lots("-0.001")


def test_book_microprice_and_depth_imbalance():
    book = PythonFixedPointOrderBook(10, [(100, 3), (99, 1)], [(102, 1), (103, 3)])
    assert book.microprice_ticks() == pytest.approx(101.5)
    assert book.depth_imbalance(2) == pytest.approx(0.0)
    assert book.depth_imbalance(1) == pytest.approx(0.5)


@pytest.mark.skipif(
    not native_available(), reason="optional Rust extension not installed"
)
def test_native_reference_parity():
    native = create_order_book(10, [(100, 5), (101, 3)], [(103, 4)])
    reference = PythonFixedPointOrderBook(10, [(100, 5), (101, 3)], [(103, 4)])
    assert exercise(native) == exercise(reference)
    assert native.depth_imbalance(5) == pytest.approx(reference.depth_imbalance(5))
    assert native.microprice_ticks() == pytest.approx(reference.microprice_ticks())


def test_instrument_aware_session_converts_snapshot_and_depth_updates():
    filters = SimpleNamespace(step_size=0.001, tick_size=0.01)
    snapshot = SimpleNamespace(
        symbol="BTCUSDT",
        bids=((65000.12, 0.125),),
        asks=((65000.14, 0.250),),
        last_update_id=100,
        timestamp=datetime.now(timezone.utc),
    )
    session = NativeOrderBookSession.from_snapshot(
        snapshot, filters, prefer_native=False
    )
    update = SimpleNamespace(
        first_update_id=100,
        final_update_id=101,
        previous_update_id=0,
        bids=((65000.12, 0.200),),
        asks=(),
    )
    assert session.apply_depth_update(update)
    update_id, bids, asks = session.snapshot(5)
    assert update_id == 101
    assert bids == [(Decimal("65000.12"), Decimal("0.200"))]
    assert asks == [(Decimal("65000.14"), Decimal("0.250"))]
    metrics = session.metrics()
    assert metrics["best_bid"] == Decimal("65000.12")
    assert metrics["best_ask"] == Decimal("65000.14")


def test_instrument_aware_session_rejects_off_tick_without_mutation():
    session = NativeOrderBookSession(
        InstrumentScale(Decimal("0.01"), Decimal("0.001")),
        100,
        [("65000.12", "0.125")],
        [("65000.14", "0.250")],
        prefer_native=False,
    )
    bad_update = SimpleNamespace(
        first_update_id=100,
        final_update_id=101,
        previous_update_id=0,
        bids=((65000.123, 0.200),),
        asks=(),
    )
    with pytest.raises(ValueError, match="not aligned"):
        session.apply_depth_update(bad_update)
    assert session.snapshot(5)[0] == 100


def test_python_reference_enforces_native_integer_widths():
    with pytest.raises(ValueError, match="uint64"):
        PythonFixedPointOrderBook(1 << 64)
    with pytest.raises(ValueError, match="int64"):
        PythonFixedPointOrderBook(10, [(1 << 63, 1)])
    with pytest.raises(ValueError, match="int64"):
        PythonFixedPointOrderBook(10, [(100, 1 << 63)])
    book = PythonFixedPointOrderBook(10, [(100, 1)], [(102, 1)])
    with pytest.raises(ValueError, match="previous_update_id"):
        book.apply_delta(11, 11, previous_update_id=-1)


def test_batch_updates_match_sequential_contract():
    updates = [
        (10, 11, [(102, 2)], [], None),
        (12, 12, [(101, 0)], [(103, 0), (104, 7)], 11),
    ]
    batch = PythonFixedPointOrderBook(10, [(100, 5), (101, 3)], [(103, 4)])
    sequential = PythonFixedPointOrderBook(10, [(100, 5), (101, 3)], [(103, 4)])
    assert batch.apply_deltas(updates) == 2
    assert sequential.apply_delta(10, 11, [(102, 2)], [], None)
    assert sequential.apply_delta(
        12, 12, [(101, 0)], [(103, 0), (104, 7)], 11
    )
    assert batch.snapshot(5) == sequential.snapshot(5)


def test_native_batch_matches_python_reference():
    if not native_available():
        pytest.skip("optional Rust extension not installed")
    updates = [
        (10, 11, [(102, 2)], [], None),
        (12, 12, [(101, 0)], [(103, 0), (104, 7)], 11),
    ]
    native = create_order_book(10, [(100, 5), (101, 3)], [(103, 4)])
    reference = PythonFixedPointOrderBook(10, [(100, 5), (101, 3)], [(103, 4)])
    assert native.apply_deltas(updates) == reference.apply_deltas(updates) == 2
    assert native.snapshot(5) == reference.snapshot(5)


def test_session_batch_converts_all_events_before_mutation():
    session = NativeOrderBookSession(
        InstrumentScale(Decimal("0.01"), Decimal("0.001")),
        100,
        [("65000.12", "0.125")],
        [("65000.14", "0.250")],
        prefer_native=False,
    )
    updates = [
        SimpleNamespace(
            first_update_id=100,
            final_update_id=101,
            previous_update_id=0,
            bids=(("65000.12", "0.200"),),
            asks=(),
        ),
        SimpleNamespace(
            first_update_id=102,
            final_update_id=102,
            previous_update_id=101,
            bids=(("65000.123", "0.100"),),
            asks=(),
        ),
    ]
    with pytest.raises(ValueError, match="not aligned"):
        session.apply_depth_updates(updates)
    assert session.snapshot(5)[0] == 100
