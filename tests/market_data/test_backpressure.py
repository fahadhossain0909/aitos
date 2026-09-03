from aitos.market_data.backpressure import BoundedMarketQueue


def test_queue_is_bounded_and_reports_drops() -> None:
    queue = BoundedMarketQueue[int](2)
    assert queue.put_nowait(1)
    assert queue.put_nowait(2)
    assert not queue.put_nowait(3)
    assert queue.qsize() == 2
    assert queue.stats.dropped == 1
    assert queue.snapshot()["depth"] == 2
