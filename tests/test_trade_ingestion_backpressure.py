import asyncio
from datetime import datetime, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


class FakeEventBus:
    def __init__(self):
        self.events = []
        self._initialized = True

    async def publish(self, event):
        self.events.append(event)


class FakeRepository:
    def __init__(self):
        self.trades = []

    async def save_trade_tick(self, trade):
        self.trades.append(trade)


@pytest.mark.asyncio
async def test_trade_batch_is_lossless_and_deduplicated():
    event_bus = FakeEventBus()
    repository = FakeRepository()
    service = DataIngestionService(
        exchange=None,
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        repository=repository,
    )

    now = datetime.now(timezone.utc)
    trades = [
        TradeTick(
            symbol="BTCUSDT",
            trade_id=i,
            price=100.0 + i * 0.01,
            quantity=1.0,
            side=TradeSide.BUY,
            is_buyer_maker=False,
            timestamp=now,
        )
        for i in range(1, 501)
    ]

    await service._process_trade_batch(trades)
    await asyncio.sleep(0)

    assert service._trade_events_received == 500
    assert service._trade_stream_dropped == 0
    assert service._trade_parse_errors == 0
    assert len(repository.trades) == 500
    assert len({trade.trade_id for trade in repository.trades}) == 500

    # Replaying the same IDs must not create duplicate state or persistence rows.
    await service._process_trade_batch(trades)
    await asyncio.sleep(0)

    assert service._trade_events_received == 500
    assert len(repository.trades) == 500
    assert service._trade_stream_dropped == 0
