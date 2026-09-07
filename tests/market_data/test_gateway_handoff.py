from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import GatewayConfig, GatewayState, MarketDataGateway


def event(source=MarketSource.WEBSOCKET, age_seconds=0.0, sequence=0):
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_id=f"test-event-{sequence}",
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_type=MarketEventType.TRADE,
        event_time=now - timedelta(seconds=age_seconds),
        ingest_time=now - timedelta(seconds=age_seconds),
        source=source,
        payload={"price": "100"},
        sequence=sequence,
    )


def test_stale_websocket_is_rejected_and_observable():
    gateway = MarketDataGateway("binance", "usd_m_futures", lambda _: None)
    assert gateway.accept(event(age_seconds=20)) is False
    snapshot = gateway.snapshot()
    assert snapshot["health"]["stale_events"] == 1
    assert snapshot["health"]["rejected_events"] == 1
    assert snapshot["state"] == GatewayState.DEGRADED.value


def test_full_queue_keeps_newest_event_without_unbounded_growth():
    gateway = MarketDataGateway(
        "binance", "usd_m_futures", lambda _: None, GatewayConfig(queue_capacity=1)
    )
    assert gateway.accept(event(sequence=1)) is True
    assert gateway.accept(event(sequence=2)) is True
    assert gateway.queue.qsize() == 1
    assert gateway.snapshot()["health"]["dropped_events"] == 1
    assert gateway.snapshot()["queue"]["replaced_oldest"] == 1


@pytest.mark.asyncio
async def test_failed_publish_is_dropped_instead_of_requeued():
    calls = 0

    async def publisher(_):
        nonlocal calls
        calls += 1
        raise RuntimeError("downstream unavailable")

    gateway = MarketDataGateway("binance", "usd_m_futures", publisher)
    gateway.begin_connect()
    gateway.mark_connected()
    assert gateway.accept(event()) is True
    with pytest.raises(RuntimeError):
        await gateway.drain_once()
    assert calls == 1
    assert gateway.queue.qsize() == 0
    assert gateway.snapshot()["health"]["publish_errors"] == 1
    assert gateway.snapshot()["health"]["dropped_events"] == 1


@pytest.mark.asyncio
async def test_old_queued_event_is_dropped_for_freshness():
    published = []

    async def publisher(item):
        published.append(item)

    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        publisher,
        GatewayConfig(max_queue_age_seconds=0.01),
    )
    gateway.begin_connect()
    gateway.mark_connected()
    assert gateway.accept(event(age_seconds=1)) is True
    await gateway.drain_once()
    assert published == []
    assert gateway.snapshot()["health"]["freshness_drops"] == 1


@pytest.mark.asyncio
async def test_successful_publish_clears_handoff():
    received = []

    async def publisher(item):
        received.append(item)

    gateway = MarketDataGateway("binance", "usd_m_futures", publisher)
    gateway.begin_connect()
    gateway.mark_connected()
    assert gateway.accept(event()) is True
    await gateway.drain_once()
    assert len(received) == 1
    assert gateway.queue.qsize() == 0
    assert gateway.snapshot()["health"]["published_events"] == 1
