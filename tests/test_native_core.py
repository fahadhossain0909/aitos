import pytest
from aitos.native_core import PythonFixedPointOrderBook, create_order_book, native_available

def exercise(book):
    assert book.apply_delta(10,11,bids=[(102,2)])
    assert book.apply_delta(12,12,bids=[(101,0)],asks=[(103,0),(104,7)],previous_update_id=11)
    return book.snapshot(5),book.best_bid_ask()

def test_reference_book_and_best_prices():
    book=PythonFixedPointOrderBook(10,[(100,5),(101,3)],[(103,4)])
    assert exercise(book)==((12,[(102,2),(100,5)],[(104,7)]),(102,104))

def test_stale_delta_is_ignored():
    book=PythonFixedPointOrderBook(10,[(100,1)],[(102,1)])
    assert not book.apply_delta(1,10,bids=[(99,5)])
    assert book.snapshot(5)==(10,[(100,1)],[(102,1)])

def test_gap_and_invalid_level_fail_without_mutation():
    book=PythonFixedPointOrderBook(10,[(100,1)],[(102,1)])
    with pytest.raises(ValueError): book.apply_delta(12,13,bids=[(101,9)])
    with pytest.raises(ValueError): book.apply_delta(11,11,bids=[(101,-1)])
    assert book.snapshot(5)==(10,[(100,1)],[(102,1)])

def test_factory_can_force_reference():
    assert isinstance(create_order_book(10,prefer_native=False),PythonFixedPointOrderBook)

@pytest.mark.skipif(not native_available(),reason="optional Rust extension not installed")
def test_native_reference_parity():
    assert exercise(create_order_book(10,[(100,5),(101,3)],[(103,4)]))==exercise(PythonFixedPointOrderBook(10,[(100,5),(101,3)],[(103,4)]))
