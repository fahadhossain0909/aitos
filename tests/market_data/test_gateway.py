from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import GatewayConfig, GatewayState, MarketDataGateway


def _event(source: MarketSource, age_seconds: float = 0) -> MarketEvent:
    return MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        payload={"price": 100},
        source=source,
    )


@pytest.mark.asyncio
async def test_gateway_rejects_stale_websocket_events() -> None:
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        lambda _: None,
        GatewayConfig(max_source_age_seconds=15),
    )
    assert gateway.accept(_event(MarketSource.WEBSOCKET, 20)) is False
    assert gateway.queue.qsize() == 0
    assert gateway.health.last_error is not None


@pytest.mark.asyncio
async def test_gateway_accepts_stale_rest_as_degraded_recovery() -> None:
    published = []
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        published.append,
        GatewayConfig(max_source_age_seconds=15),
    )
    gateway.begin_connect()
    gateway.mark_connected()
    assert gateway.accept(_event(MarketSource.REST, 20)) is True
    assert gateway.state is GatewayState.DEGRADED
    assert gateway.health.degraded is True


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
    assert gateway.accept(_event(MarketSource.WEBSOCKET))
    assert gateway.snapshot()["queue"]["depth"] == 1
    await gateway.drain_once()
    assert gateway.state is GatewayState.CONNECTED
    assert len(published) == 1
    assert gateway.snapshot()["queue"]["depth"] == 0
