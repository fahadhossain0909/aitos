import asyncio
from datetime import datetime, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


class FailingEventBus:
    def __init__(self, fail_topic: str):
        self.fail_topic = fail_topic
        self.calls = []
        self._initialized = True

    async def publish(self, event):
        self.calls.append(event.topic)
        if event.topic == self.fail_topic:
            raise RuntimeError(f"intentional sink failure: {event.topic}")


class RecordingRepository:
    def __init__(self):
        self.trades = []

    async def save_trade_tick(self, trade):
        self.trades.append(trade)


@pytest.mark.asyncio
async def test_trade_downstream_failure_isolated_per_sink():
    """A single sink failure must be diagnosable without hiding other sink work."""
    event_bus = FailingEventBus("market.orderflow.BTCUSDT")
    repository = RecordingRepository()
    service = DataIngestionService(
        exchange=None,
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        repository=repository,
    )

    trade = TradeTick(
        symbol="BTCUSDT",
        trade_id=1,
        price=100.0,
        quantity=1.0,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=datetime.now(timezone.utc),
    )

    await service._process_trade_batch([trade])
    await asyncio.sleep(0)

    assert service._trade_events_received == 1
    assert service._trade_downstream_errors == 1
    assert "market.trade.BTCUSDT" in event_bus.calls
    assert "market.orderflow.BTCUSDT" in event_bus.calls
    assert len(repository.trades) == 1
