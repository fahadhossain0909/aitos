import asyncio
from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.persistence_sink import CanonicalMarketDataPersistenceSink


class FakeRepository:
    def __init__(self):
        self.trades = []
        self.books = []
        self.depth_events = []
        self.versioned_events = []
        self.versioned_batch_calls = 0

    async def save_market_event_versioned(self, event):
        self.versioned_events.append(event)

    async def save_market_events_versioned(self, events):
        self.versioned_batch_calls += 1
        self.versioned_events.extend(events)

    async def save_depth_events(self, events):
        self.depth_events.extend(events)

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
async def test_history_queue_overflow_fails_handler_so_source_is_not_acked():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=1)

    await sink._enqueue(trade_event(9))
    with pytest.raises(RuntimeError, match="queue is full"):
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
    assert sink._queue.get_nowait()[0].symbol == "BTCUSDT"


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
    with pytest.raises(RuntimeError, match="queue is full"):
        await sink._enqueue(trade_event(31))

    capacity = sink.snapshot()["root_cause_telemetry"]["capacity"]
    assert capacity["enqueued"] == 1
    assert capacity["rejected"] == 1
    assert capacity["max_queue_depth"] == 1


@pytest.mark.asyncio
async def test_book_delta_is_persisted_without_snapshot_sampling():
    repo = FakeRepository()
    sink = make_sink(repo)
    delta = event(
        MarketEventType.BOOK_DELTA,
        {
            "first_update_id": 101,
            "final_update_id": 101,
            "previous_update_id": 100,
            "bids": [[100.0, 3.0]],
            "asks": [[101.0, 4.0]],
        },
    )
    await sink._enqueue(delta)
    assert sink.snapshot()["queue_depth"] == 1
    assert sink.snapshot()["filtered"] == 0

    await sink._persist_batch([delta])

    assert len(repo.depth_events) == 1
    assert repo.depth_events[0]["symbol"] == "BTCUSDT"
    assert repo.depth_events[0]["payload"]["final_update_id"] == 101


@pytest.mark.asyncio
async def test_spot_ticker_is_persisted_with_market_identity_and_revision():
    repo = FakeRepository()
    sink = make_sink(repo)
    now = datetime.now(timezone.utc)
    spot_ticker = MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="spot",
        market_type="spot",
        instrument_id="binance:spot:BTCUSDT",
        symbol="BTCUSDT",
        event_time=now,
        ingest_time=now,
        payload={"last_price": "90000.5", "event_time_ms": 123456789},
        source=MarketSource.WEBSOCKET,
        event_revision=3,
    )
    await sink._enqueue(spot_ticker)
    assert sink.snapshot()["queue_depth"] == 1

    await sink._persist_batch([spot_ticker])

    assert repo.versioned_events == [spot_ticker]
    persisted = repo.versioned_events[0]
    assert persisted.market_type == "spot"
    assert persisted.instrument_id == "binance:spot:BTCUSDT"
    assert persisted.event_revision == 3
    assert persisted.payload["last_price"] == "90000.5"
    assert sink.snapshot()["versioned_events"] == 1


def test_market_event_rejects_non_positive_revision():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="event_revision"):
        MarketEvent(
            event_type=MarketEventType.TICKER,
            exchange="binance",
            market="spot",
            symbol="BTCUSDT",
            event_time=now,
            ingest_time=now,
            payload={},
            source=MarketSource.WEBSOCKET,
            event_revision=0,
        )


@pytest.mark.asyncio
async def test_canonical_only_spot_persistence_skips_legacy_symbol_tables():
    repo = FakeRepository()
    sink = CanonicalMarketDataPersistenceSink(
        None,
        repo,
        historical_trade_symbols=("BTCUSDT",),
        market_types=("spot",),
        canonical_only=True,
    )
    now = datetime.now(timezone.utc)
    spot_trade = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="spot",
        market_type="spot",
        instrument_id="binance:spot:BTCUSDT",
        symbol="BTCUSDT",
        event_time=now,
        ingest_time=now,
        payload={
            "symbol": "BTCUSDT",
            "trade_id": 71,
            "price": 90000.0,
            "quantity": 0.1,
            "side": "BUY",
            "is_buyer_maker": False,
            "timestamp": now.isoformat(),
        },
        source=MarketSource.WEBSOCKET,
    )
    await sink._persist_batch([spot_trade])
    assert repo.versioned_events == [spot_trade]
    assert repo.trades == []


@pytest.mark.asyncio
async def test_shutdown_flushes_queued_canonical_events_before_worker_exit():
    repo = FakeRepository()
    sink = CanonicalMarketDataPersistenceSink(
        None,
        repo,
        canonical_only=True,
        shutdown_flush_seconds=1.0,
    )
    spot_ticker = MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="spot",
        market_type="spot",
        instrument_id="binance:spot:ETHUSDT",
        symbol="ETHUSDT",
        event_time=datetime.now(timezone.utc),
        payload={"last_price": "3000"},
        source=MarketSource.WEBSOCKET,
    )
    completion = asyncio.get_running_loop().create_future()
    await sink._queue.put((spot_ticker, completion))
    sink._workers = [asyncio.create_task(sink._worker(0))]
    await sink.shutdown()
    assert repo.versioned_events == [spot_ticker]
    assert sink.snapshot()["shutdown_dropped"] == 0


@pytest.mark.asyncio
async def test_versioned_events_use_one_bulk_insert_per_persistence_batch():
    repo = FakeRepository()
    sink = make_sink(repo)
    events = [trade_event(101), trade_event(102), trade_event(103)]

    await sink._persist_batch(events)

    assert repo.versioned_batch_calls == 1
    assert repo.versioned_events == events
    assert sink.snapshot()["versioned_events"] == 3


@pytest.mark.asyncio
async def test_persistence_sink_reports_latency_per_target_table():
    repo = FakeRepository()
    sink = make_sink(repo)

    await sink._persist_batch([trade_event(401)])

    table_latency = sink.snapshot()["table_write_latency"]
    assert table_latency["market_event_versions"]["count"] == 1
    assert table_latency["trade_ticks"]["count"] == 1
    assert table_latency["market_event_versions"]["p95_ms"] is not None
    assert table_latency["trade_ticks"]["p95_ms"] is not None


@pytest.mark.asyncio
async def test_multi_event_batch_refuses_single_event_insert_fallback():
    repo = FakeRepository()
    repo.save_market_events_versioned = None
    sink = make_sink(repo)

    with pytest.raises(RuntimeError, match="requires the bulk"):
        await sink._persist_batch([trade_event(301), trade_event(302)])

    assert repo.versioned_events == []


@pytest.mark.asyncio
async def test_redis_handler_waits_for_durable_persistence_before_ack_boundary():
    repo = FakeRepository()
    sink = make_sink(repo)
    worker = asyncio.create_task(sink._worker(0))
    try:
        event = trade_event(201)
        await asyncio.wait_for(sink._persist_before_ack(event), timeout=1.0)
        assert repo.versioned_events == [event]
        snapshot = sink.snapshot()
        assert snapshot["queue_depth"] == 0
        assert snapshot["processed"] == 1
        assert snapshot["queue_wait"]["count"] == 1
        assert snapshot["queue_wait"]["p50_ms"] is not None
        assert snapshot["batch_write"]["count"] == 1
        assert snapshot["batch_write"]["p95_ms"] is not None
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_persistence_failure_propagates_to_redis_handler_for_retry():
    class FailingRepository(FakeRepository):
        async def save_market_events_versioned(self, events):
            raise ConnectionError("ClickHouse unavailable")

    sink = make_sink(FailingRepository())
    worker = asyncio.create_task(sink._worker(0))
    try:
        with pytest.raises(ConnectionError, match="ClickHouse unavailable"):
            await asyncio.wait_for(
                sink._persist_before_ack(trade_event(301)), timeout=1.0
            )
        assert sink.snapshot()["errors"] == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
