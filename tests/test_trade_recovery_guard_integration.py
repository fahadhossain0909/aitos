from datetime import datetime, timedelta, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


def trade(trade_id: int, age_seconds: float) -> TradeTick:
    now = datetime.now(timezone.utc)
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=65000.0,
        quantity=0.01,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=now - timedelta(seconds=age_seconds),
    )


class GuardExchange:
    async def connect(self):
        return None

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_recovery_guard_rejects_new_id_with_old_timestamp(event_bus):
    service = DataIngestionService(
        exchange=GuardExchange(), event_bus=event_bus, symbols=["BTCUSDT"]
    )
    service._transport_rest_recovery_active = True
    service._last_trade_ids["BTCUSDT"] = 100
    service._trade_recovery_source_timestamps["BTCUSDT"] = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    )

    await service._process_trade_batch([trade(101, 30.0)])

    assert service._last_trade_ids["BTCUSDT"] == 100
    assert service._trade_recovery_guard_rejected == 1


@pytest.mark.asyncio
async def test_recovery_guard_accepts_fresh_monotonic_trade(event_bus):
    service = DataIngestionService(
        exchange=GuardExchange(), event_bus=event_bus, symbols=["BTCUSDT"]
    )
    service._transport_rest_recovery_active = True
    service._last_trade_ids["BTCUSDT"] = 100
    service._trade_recovery_source_timestamps["BTCUSDT"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )

    await service._process_trade_batch([trade(101, 1.0)])

    assert service._last_trade_ids["BTCUSDT"] == 101
    assert service._trade_recovery_guard_rejected == 0
