import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from aitos.core.contracts import Event
from aitos.data.ingestion import (
    DataIngestionService,
    kline_topic,
    orderbook_topic,
    trade_topic,
)
from aitos.exchange.base import ExchangeAdapter
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)

NOW = datetime.now(timezone.utc)

SAMPLE_KLINE = Kline(
    symbol="BTCUSDT",
    timeframe="1m",
    open_time=NOW,
    close_time=NOW,
    open=100.0,
    high=101.0,
    low=99.0,
    close=100.5,
    volume=5.0,
    quote_volume=500.0,
    trades_count=10,
    taker_buy_volume=2.0,
    taker_buy_quote_volume=200.0,
)
SAMPLE_TRADE = TradeTick(
    symbol="BTCUSDT",
    trade_id=1,
    price=100.0,
    quantity=1.0,
    side=TradeSide.BUY,
    is_buyer_maker=False,
    timestamp=NOW,
)
SAMPLE_BOOK = OrderBookSnapshot(
    symbol="BTCUSDT",
    bids=((99.5, 1.0),),
    asks=((100.5, 1.0),),
    last_update_id=1,
    timestamp=NOW,
)


class FakeExchangeAdapter(ExchangeAdapter):
    """Yields exactly one of each event type, then idles until cancelled."""

    def __init__(self):
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def fetch_klines(self, symbol, timeframe, limit=500) -> list[Kline]:
        return [SAMPLE_KLINE, SAMPLE_KLINE]

    async def fetch_order_book(self, symbol, limit=50) -> OrderBookSnapshot:
        return SAMPLE_BOOK

    async def fetch_recent_trades(self, symbol, limit=500) -> list[TradeTick]:
        return [SAMPLE_TRADE]

    async def fetch_funding_rate(self, symbol) -> FundingRate:
        raise NotImplementedError

    async def fetch_open_interest(self, symbol) -> OpenInterest:
        raise NotImplementedError

    async def stream_klines(self, symbols, timeframe) -> AsyncIterator[Kline]:
        yield SAMPLE_KLINE
        await asyncio.sleep(3600)

    async def stream_trades(self, symbols) -> AsyncIterator[TradeTick]:
        yield SAMPLE_TRADE
        await asyncio.sleep(3600)

    async def stream_order_book(
        self, symbols, levels=20
    ) -> AsyncIterator[OrderBookSnapshot]:
        yield SAMPLE_BOOK
        await asyncio.sleep(3600)


class FakeRepository:
    def __init__(self):
        self.klines = []
        self.trades = []
        self.books = []

    async def save_kline(self, kline):
        self.klines.append(kline)

    async def save_trade_tick(self, trade):
        self.trades.append(trade)

    async def save_orderbook_snapshot(self, book):
        self.books.append(book)


@pytest.mark.asyncio
async def test_ingestion_publishes_events_and_persists(event_bus):
    exchange = FakeExchangeAdapter()
    repository = FakeRepository()
    service = DataIngestionService(
        exchange=exchange,
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        kline_timeframe="1m",
        repository=repository,
    )

    received_topics = []

    async def handler(event: Event):
        received_topics.append(event.topic)

    await event_bus.subscribe(kline_topic("BTCUSDT", "1m"), handler, group="test")
    await event_bus.subscribe(trade_topic("BTCUSDT"), handler, group="test")
    await event_bus.subscribe(orderbook_topic("BTCUSDT"), handler, group="test")

    await service.initialize({})

    for _ in range(30):
        if (
            len(received_topics) >= 3
            and len(repository.klines)
            and len(repository.trades)
            and len(repository.books)
        ):
            break
        await asyncio.sleep(0.1)

    assert kline_topic("BTCUSDT", "1m") in received_topics
    assert trade_topic("BTCUSDT") in received_topics
    assert orderbook_topic("BTCUSDT") in received_topics
    assert len(repository.klines) == 1
    assert len(repository.trades) == 1
    assert len(repository.books) == 1
    assert exchange.connected is True

    health = await service.health_check()
    assert health.details["ticks_processed"] == 3
    assert health.details["trade_stream_messages_received"] == 1
    assert health.details["trade_events_received"] == 1
    assert health.details["trade_parse_errors"] == 0
    assert health.details["trade_stream_errors"] == 0
    assert health.details["last_trade_event_time"] is not None

    await service.shutdown(grace_period_seconds=2.0)
    assert exchange.closed is True


@pytest.mark.asyncio
async def test_backfill_klines_publishes_and_persists_history(event_bus):
    exchange = FakeExchangeAdapter()
    repository = FakeRepository()
    service = DataIngestionService(
        exchange=exchange,
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        repository=repository,
    )
    await service.initialize({})

    count = await service.backfill_klines("BTCUSDT", "1m", limit=2)

    assert count == 2
    assert len(repository.klines) >= 2

    await service.shutdown(grace_period_seconds=2.0)


@pytest.mark.asyncio
async def test_trade_cursor_is_not_advanced_when_live_state_update_fails(event_bus):
    exchange = FakeExchangeAdapter()
    service = DataIngestionService(
        exchange=exchange,
        event_bus=event_bus,
        symbols=["BTCUSDT"],
    )
    await service.initialize({})

    original_on_trade = service._live_state.on_trade
    calls = 0

    def flaky_on_trade(trade):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic live-state failure")
        return original_on_trade(trade)

    service._live_state.on_trade = flaky_on_trade
    await service._process_trade_batch([SAMPLE_TRADE])
    assert service._last_trade_ids.get("BTCUSDT") is None

    await service._process_trade_batch([SAMPLE_TRADE])
    assert service._last_trade_ids.get("BTCUSDT") == SAMPLE_TRADE.trade_id
    assert service._trade_parse_errors == 1

    await service.shutdown(grace_period_seconds=2.0)
