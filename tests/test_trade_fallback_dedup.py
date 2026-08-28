import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


class FakeExchange:
    async def connect(self):
        return None

    async def close(self):
        return None


def trade(trade_id: int, seconds_ago: float) -> TradeTick:
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=65000.0,
        quantity=0.01,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    )


@pytest.mark.asyncio
async def test_raw_fallback_trade_after_aggtrade_cursor_is_accepted(event_bus):
    service = DataIngestionService(
        exchange=FakeExchange(),
        event_bus=event_bus,
        symbols=["BTCUSDT"],
    )
    await service.initialize({})

    # The aggregate parser now stores the last raw trade ID covered by the
    # aggregate. A subsequent @trade fallback event must therefore advance
    # the same cursor instead of being discarded as "old".
    aggregate_canonical = trade(105, 1.0)
    fallback_raw = trade(106, 0.5)

    await service._process_trade_batch([aggregate_canonical])
    await service._process_trade_batch([fallback_raw])

    assert service._last_trade_ids["BTCUSDT"] == 106
    assert service._trade_events_received == 2
    assert service._trade_stream_dropped == 0

    await service.shutdown(grace_period_seconds=2.0)
