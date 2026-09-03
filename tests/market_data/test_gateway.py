from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import GatewayConfig, GatewayState, MarketDataGateway


@pytest.mark.asyncio
async def test_gateway_rejects_stale_events() -> None:
    published = []
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        published.append,
        GatewayConfig(queue_capacity=2, max_source_age_seconds=15),
    )
    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc) - timedelta(seconds=20),
        payload={"price": 100},
        source=MarketSource.REST,
    )
    assert gateway.accept(event) is False
    assert gateway.queue.qsize() == 0
    assert gateway.health.last_error is not None


@pytest.mark.asyncio
async def test_gateway_publishes_and_exposes_queue_depth() -> None:
    published = []

    async def publish(event):
        published.append(event)

    gateway = MarketDataGateway(
        "binance", "usd_m_futures", publish, GatewayConfig(queue_capacity=1)
    )
    gateway.begin_connect()
    gateway.mark_connected()
    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc),
        payload={"price": 100},
        source=MarketSource.WEBSOCKET,
    )
    assert gateway.accept(event)
    assert gateway.snapshot()["queue"]["depth"] == 1
    await gateway.drain_once()
    assert gateway.state is GatewayState.CONNECTED
    assert len(published) == 1
    assert gateway.snapshot()["queue"]["depth"] == 0
