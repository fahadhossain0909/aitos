from datetime import datetime, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


class Exchange:
    async def connect(self):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_direct_live_path_admits_trade(event_bus):
    received = []
    trade = TradeTick(
        symbol="BTCUSDT",
        trade_id=123,
        price=100.0,
        quantity=1.0,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=datetime.now(timezone.utc),
    )

    async def handler(t):
        received.append(t.trade_id)

    service = DataIngestionService(
        exchange=Exchange(),
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        live_trade_handler=handler,
    )
    await service.initialize({})
    await service._process_trade_batch([trade])
    assert received == [123]
    assert service._trade_persistence_queue.qsize() == 1
    await service.shutdown(grace_period_seconds=2.0)
