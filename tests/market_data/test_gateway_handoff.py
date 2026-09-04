from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketSource
from aitos.market_data.gateway import GatewayConfig, MarketDataGateway, GatewayState


def event(source=MarketSource.WEBSOCKET, age_seconds=0.0):
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_id="test-event",
        symbol="BTCUSDT",
        event_type="trade",
        event_time=now - timedelta(seconds=age_seconds),
        ingest_time=now,
        source=source,
        payload={"price": "100"},
    )


def test_stale_websocket_is_rejected_and_observable():
    gateway = MarketDataGateway("binance", "usd_m_futures", lambda _: None)
    assert gateway.accept(event(age_seconds=20)) is False
    snapshot = gateway.snapshot()
    assert snapshot["health"]["stale_events"] == 1
    assert snapshot["health"]["rejected_events"] == 1
    assert snapshot["state"] == GatewayState.DEGRADED.value


def test_full_queue_is_rejected_without_unbounded_growth():
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        lambda _: None,
        GatewayConfig(queue_capacity=1),
    )
    assert gateway.accept(event()) is True
    assert gateway.accept(event()) is False
    assert gateway.queue.qsize() == 1
    assert gateway.snapshot()["health"]["dropped_events"] == 1


@pytest.mark.asyncio
async def test_failed_publish_is_retried_and_remains_pending():
    calls = 0

    async def publisher(_):
        nonlocal calls
        calls += 1
        raise RuntimeError("downstream unavailable")

    gateway = MarketDataGateway("binance", "usd_m_futures", publisher)
    assert gateway.accept(event()) is True
    with pytest.raises(RuntimeError):
        await gateway.drain_once()
    assert calls == 1
    assert gateway.queue.qsize() == 1
    assert gateway.snapshot()["health"]["publish_errors"] == 1


@pytest.mark.asyncio
async def test_successful_publish_clears_handoff():
    received = []

    async def publisher(item):
        received.append(item)

    gateway = MarketDataGateway("binance", "usd_m_futures", publisher)
    assert gateway.accept(event()) is True
    await gateway.drain_once()
    assert len(received) == 1
    assert gateway.queue.qsize() == 0
    assert gateway.snapshot()["health"]["published_events"] == 1
