from aitos.market_data.backpressure import BoundedMarketQueue


def test_queue_is_bounded_and_reports_drops() -> None:
    queue = BoundedMarketQueue[int](2)
    assert queue.put_nowait(1)
    assert queue.put_nowait(2)
    assert not queue.put_nowait(3)
    assert queue.qsize() == 2
    assert queue.stats.dropped == 1
    assert queue.snapshot()["depth"] == 2


def test_queue_coalesces_latest_value_by_key_without_dropping_other_items() -> None:
    queue = BoundedMarketQueue[tuple[str, int]](3)
    assert queue.put_nowait(("BTCUSDT", 1))
    assert queue.put_nowait(("ETHUSDT", 1))
    assert queue.put_coalesced_nowait(
        ("BTCUSDT", 2), key=lambda item: item[0]
    )

    assert list(queue._queue._queue) == [("ETHUSDT", 1), ("BTCUSDT", 2)]
    assert queue.stats.coalesced == 1
    assert queue.snapshot()["coalesced"] == 1
    assert queue.stats.depth == 2
