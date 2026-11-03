from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.persistence_sink import CanonicalMarketDataPersistenceSink


class FakeRepository:
    def __init__(self):
        self.trades = []
        self.books = []

    async def save_trade_ticks(self, trades):
        self.trades.extend(trades)

    async def save_order_book_snapshots(self, books):
        self.books.extend(books)


def event(event_type, payload, symbol="BTCUSDT"):
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_type=event_type,
        exchange="binance",
        market="usd_m_futures",
        market_type="usd_m_futures",
        symbol=symbol,
        event_time=now,
        payload=payload,
        source=MarketSource.WEBSOCKET,
        ingest_time=now,
    )


def make_sink(repo, queue_capacity=10_000):
    return CanonicalMarketDataPersistenceSink(
        None,
        repo,
        queue_capacity=queue_capacity,
        workers=1,
    )


def trade_event(trade_id=7, symbol="BTCUSDT"):
    return event(
        MarketEventType.TRADE,
        {
            "symbol": symbol,
            "trade_id": trade_id,
            "price": 90000.0,
            "quantity": 1.0,
            "side": "BUY",
            "is_buyer_maker": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        symbol=symbol,
    )


@pytest.mark.asyncio
async def test_persistence_sink_batches_trade():
    repo = FakeRepository()
    sink = make_sink(repo)
    await sink._persist_batch([trade_event()])
    assert len(repo.trades) == 1
    assert repo.trades[0].trade_id == 7


@pytest.mark.asyncio
async def test_non_anchor_book_is_filtered_before_persistence():
    repo = FakeRepository()
    sink = make_sink(repo)
    await sink._enqueue(
        event(
            MarketEventType.BOOK_SNAPSHOT,
            {
                "bids": [{"price": 99.0, "quantity": 2.0}],
                "asks": [{"price": 101.0, "quantity": 2.0}],
                "last_update_id": 8,
            },
            symbol="ETHUSDT",
        )
    )
    assert sink.snapshot()["queue_depth"] == 0
    assert sink.snapshot()["filtered"] == 1
    assert repo.books == []


@pytest.mark.asyncio
async def test_history_enqueue_never_waits_for_clickhouse():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=2)

    await sink._enqueue(trade_event(8))

    assert sink.snapshot()["queue_depth"] == 1
    assert sink.snapshot()["rejected"] == 0
    assert repo.trades == []


@pytest.mark.asyncio
async def test_non_anchor_trade_is_filtered_from_history_queue():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=2)

    await sink._enqueue(trade_event(99, symbol="ETHUSDT"))

    assert sink.snapshot()["queue_depth"] == 0
    assert sink.snapshot()["filtered"] == 1


@pytest.mark.asyncio
async def test_history_queue_overflow_drops_history_instead_of_backpressuring():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=1)

    await sink._enqueue(trade_event(9))
    await sink._enqueue(trade_event(10))

    assert sink.snapshot()["queue_depth"] == 1
    assert sink.snapshot()["rejected"] == 1
    assert repo.trades == []


@pytest.mark.asyncio
async def test_only_btc_ltc_books_enter_history_queue():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=4)

    await sink._enqueue(
        event(
            MarketEventType.BOOK_SNAPSHOT,
            {
                "bids": [{"price": 99.0, "quantity": 2.0}],
                "asks": [{"price": 101.0, "quantity": 2.0}],
                "last_update_id": 11,
            },
            symbol="ETHUSDT",
        )
    )
    await sink._enqueue(
        event(
            MarketEventType.BOOK_SNAPSHOT,
            {
                "bids": [{"price": 99.0, "quantity": 2.0}],
                "asks": [{"price": 101.0, "quantity": 2.0}],
                "last_update_id": 12,
            },
            symbol="BTCUSDT",
        )
    )

    assert sink.snapshot()["queue_depth"] == 1
    assert sink._queue.get_nowait().symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_persistence_capacity_telemetry_tracks_admission_and_batch_size():
    # The forensics module installs the persistence-sink instrumentation at
    # import time in the normal application startup path.
    import aitos.forensics.root_cause_telemetry  # noqa: F401

    repo = FakeRepository()
    sink = make_sink(repo)
    first = trade_event(20)
    second = trade_event(21)

    await sink._enqueue(first)
    await sink._enqueue(second)
    await sink._persist_batch([first, second])

    telemetry = sink.snapshot()["root_cause_telemetry"]
    capacity = telemetry["capacity"]
    assert capacity["enqueued"] == 2
    assert capacity["rejected"] == 0
    assert capacity["batches"] == 1
    assert capacity["batch_events"] == 2
    assert capacity["avg_batch_size"] == 2.0
    assert capacity["min_batch_size"] == 2
    assert capacity["max_batch_size"] == 2
    assert telemetry["batch_write"]["count"] == 1


@pytest.mark.asyncio
async def test_persistence_capacity_telemetry_tracks_rejected_history():
    import aitos.forensics.root_cause_telemetry  # noqa: F401

    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=1)
    await sink._enqueue(trade_event(30))
    await sink._enqueue(trade_event(31))

    capacity = sink.snapshot()["root_cause_telemetry"]["capacity"]
    assert capacity["enqueued"] == 1
    assert capacity["rejected"] == 1
    assert capacity["max_queue_depth"] == 1
