from datetime import datetime, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import OrderBookSnapshot


class Exchange:
    async def connect(self):
        pass

    async def close(self):
        pass

    async def stream_klines(self, symbols, timeframe):
        if False:
            yield None

    async def stream_trades(self, symbols):
        if False:
            yield None

    async def stream_order_book(self, symbols, levels=20):
        if False:
            yield None


@pytest.mark.asyncio
async def test_direct_live_orderbook_path_admits_snapshot(event_bus):
    received = []
    book = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 1.0),),
        asks=((101.0, 1.0),),
        last_update_id=456,
        timestamp=datetime.now(timezone.utc),
    )

    async def handler(snapshot):
        received.append(snapshot.last_update_id)

    service = DataIngestionService(
        exchange=Exchange(),
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        live_orderbook_handler=handler,
    )
    await service.initialize({})
    await service._handle_order_book(book)

    assert received == [456]
    await service.shutdown(grace_period_seconds=2.0)
