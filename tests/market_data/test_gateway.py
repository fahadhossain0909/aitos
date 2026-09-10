import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import GatewayConfig, GatewayState, MarketDataGateway


def _event(
    source: MarketSource, age_seconds: float = 0, sequence: int = 0
) -> MarketEvent:
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=now - timedelta(seconds=age_seconds),
        ingest_time=now - timedelta(seconds=age_seconds),
        payload={"price": 100, "sequence": sequence},
        source=source,
        sequence=sequence,
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


@pytest.mark.asyncio
async def test_gateway_preserves_fifo_order_with_single_drain() -> None:
    published = []

    async def publish(event):
        published.append(event.sequence)

    gateway = MarketDataGateway(
        "binance", "usd_m_futures", publish, GatewayConfig(queue_capacity=8)
    )
    gateway.begin_connect()
    gateway.mark_connected()

    for sequence in range(5):
        assert gateway.accept(_event(MarketSource.WEBSOCKET, sequence=sequence))
    for _ in range(5):
        await gateway.drain_once()

    assert published == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_gateway_drain_stage_telemetry_breaks_out_publisher() -> None:
    async def publish(event):
        await asyncio.sleep(0)

    gateway = MarketDataGateway("binance", "usd_m_futures", publish)
    gateway.begin_connect()
    gateway.mark_connected()
    assert gateway.accept(_event(MarketSource.WEBSOCKET))

    import aitos.forensics.root_cause_telemetry as telemetry

    telemetry.install()
    await gateway.drain_once()
    stages = gateway.snapshot()["root_cause_telemetry"]["gateway_drain_stages"]
    assert set(stages) >= {
        "queue_get",
        "queue_age_check",
        "publisher",
        "queue_task_done",
    }
    assert stages["publisher"]["count"] == 1
