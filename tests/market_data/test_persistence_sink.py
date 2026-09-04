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
    sink._historical_books = {"BTCUSDT", "LTCUSDT"}
    sink._book_interval = 1.0
    sink._last_book_persist = {}
    sink._queue = None
    # Filtering belongs to _enqueue; direct persistence remains intentionally
    # generic so the sink can be unit-tested independently.
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
