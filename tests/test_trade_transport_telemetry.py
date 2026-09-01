from datetime import datetime, timedelta, timezone

import pytest

from aitos.data.ingestion import DataIngestionService
from aitos.models.market import TradeSide, TradeTick


class TelemetryExchange:
    async def connect(self):
        return None

    async def close(self):
        return None

    async def fetch_recent_trades(self, symbol: str, limit: int = 500):
        return [trade(101, 1.0)]


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
async def test_transport_telemetry_records_both_downstream_paths(event_bus):
    direct_events: list[int] = []

    async def direct_handler(item: TradeTick) -> None:
        direct_events.append(item.trade_id)

    service = DataIngestionService(
        exchange=TelemetryExchange(),
        event_bus=event_bus,
        symbols=["BTCUSDT"],
        live_trade_handler=direct_handler,
    )
    await service.initialize({})

    await service._recover_recent_trades()
    assert service._transport_mode == "rest_fallback"
    assert service._transport_fallback_count == 1
    assert service._transport_rest_batches == 1
    assert service._transport_rest_trades_recovered == 1
    assert service._transport_rest_direct_events == 1
    assert service._transport_rest_direct_errors == 0

    await service._process_trade_batch([trade(102, 0.5)])
    assert service._transport_mode == "websocket"
    assert service._transport_recovery_count == 1
    assert service._transport_ws_batches == 1
    assert service._transport_ws_direct_events == 1
    assert service._transport_ws_direct_errors == 0
    assert service._transport_last_recovery_at is not None
    assert service._transport_fallback_active_seconds == 0.0
    assert direct_events == [101, 102]

    health = await service.health_check()
    assert health.details["transport_mode"] == "websocket"
    assert health.details["transport_fallback_count"] == 1
    assert health.details["transport_recovery_count"] == 1
    assert health.details["transport_rest_trades_recovered"] == 1
    assert health.details["transport_ws_direct_events"] == 1
    assert health.details["transport_rest_direct_events"] == 1

    await service.shutdown(grace_period_seconds=2.0)
