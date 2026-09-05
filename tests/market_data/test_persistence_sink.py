from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.persistence_sink import CanonicalMarketDataPersistenceSink


class FakeRepository:
    def __init__(self):
        self.trades = []
        self.books = []
        self.klines = []

    async def save_trade_tick(self, trade):
        self.trades.append(trade)

    async def save_order_book_snapshot(self, book):
        self.books.append(book)

    async def save_kline(self, kline):
        self.klines.append(kline)


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


@pytest.mark.asyncio
async def test_persistence_sink_persists_trade():
    repo = FakeRepository()
    sink = CanonicalMarketDataPersistenceSink.__new__(
        CanonicalMarketDataPersistenceSink
    )
    sink._repository = repo
    await sink._persist(
        event(
            MarketEventType.TRADE,
            {
                "symbol": "BTCUSDT",
                "trade_id": 7,
                "price": 90000.0,
                "quantity": 1.0,
                "side": "BUY",
                "is_buyer_maker": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    assert len(repo.trades) == 1
    assert repo.trades[0].trade_id == 7


@pytest.mark.asyncio
async def test_persistence_sink_does_not_persist_non_anchor_books():
    repo = FakeRepository()
    sink = CanonicalMarketDataPersistenceSink.__new__(
        CanonicalMarketDataPersistenceSink
    )
    sink._repository = repo
    await sink._persist(
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
    assert len(repo.books) == 1


@pytest.mark.asyncio
async def test_history_enqueue_never_waits_for_clickhouse():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=2)

    await sink._enqueue(
        event(
            MarketEventType.TRADE,
            {
                "symbol": "BTCUSDT",
                "trade_id": 8,
                "price": 90001.0,
                "quantity": 1.0,
                "side": "BUY",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    assert sink.snapshot()["queue_depth"] == 1
    assert sink.snapshot()["rejected"] == 0
    assert repo.trades == []


@pytest.mark.asyncio
async def test_history_queue_overflow_drops_history_instead_of_backpressuring():
    repo = FakeRepository()
    sink = make_sink(repo, queue_capacity=1)

    first = event(
        MarketEventType.TRADE,
        {
            "symbol": "BTCUSDT",
            "trade_id": 9,
            "price": 90002.0,
            "quantity": 1.0,
            "side": "BUY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    second = event(
        MarketEventType.TRADE,
        {
            "symbol": "BTCUSDT",
            "trade_id": 10,
            "price": 90003.0,
            "quantity": 1.0,
            "side": "BUY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    await sink._enqueue(first)
    await sink._enqueue(second)

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
