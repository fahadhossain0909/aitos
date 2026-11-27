import asyncio
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.gateway import GatewayConfig, GatewayState, MarketDataGateway


def _event(
    source: MarketSource,
    age_seconds: float = 0,
    sequence: int = 0,
    event_type: MarketEventType = MarketEventType.TRADE,
) -> MarketEvent:
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_type=event_type,
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


@pytest.mark.asyncio
async def test_gateway_drain_once_returns_false_on_empty_queue() -> None:
    published = []

    async def publish(event):
        published.append(event)

    gateway = MarketDataGateway(
        "binance", "usd_m_futures", publish, GatewayConfig(queue_capacity=4)
    )
    assert await gateway.drain_once() is False
    assert published == []
    assert gateway.queue.qsize() == 0


@pytest.mark.asyncio
async def test_gateway_never_evicts_queued_orderbook_delta_for_trade_burst() -> None:
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        lambda _: None,
        GatewayConfig(queue_capacity=1),
    )
    gateway.begin_connect()
    gateway.mark_connected()
    delta = _event(
        MarketSource.WEBSOCKET,
        sequence=10,
        event_type=MarketEventType.BOOK_DELTA,
    )
    trade = _event(MarketSource.WEBSOCKET, sequence=11)

    assert gateway.accept(delta) is True
    assert gateway.accept(trade) is False
    assert gateway.queue.qsize() == 1
    assert gateway.queue._queue._queue[0] is delta


@pytest.mark.asyncio
async def test_gateway_admits_book_delta_by_evicting_replaceable_trade() -> None:
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        lambda _: None,
        GatewayConfig(queue_capacity=1),
    )
    gateway.begin_connect()
    gateway.mark_connected()
    trade = _event(MarketSource.WEBSOCKET, sequence=10)
    delta = _event(
        MarketSource.WEBSOCKET,
        sequence=11,
        event_type=MarketEventType.BOOK_DELTA,
    )

    assert gateway.accept(trade) is True
    assert gateway.accept(delta) is True
    assert gateway.queue._queue._queue[0] is delta
    assert gateway.queue.stats.replaced_oldest == 1


@pytest.mark.asyncio
async def test_gateway_degrades_when_all_queue_slots_are_sequence_critical() -> None:
    gateway = MarketDataGateway(
        "binance",
        "usd_m_futures",
        lambda _: None,
        GatewayConfig(queue_capacity=1),
    )
    gateway.begin_connect()
    gateway.mark_connected()
    first = _event(
        MarketSource.WEBSOCKET,
        sequence=10,
        event_type=MarketEventType.BOOK_DELTA,
    )
    second = _event(
        MarketSource.WEBSOCKET,
        sequence=11,
        event_type=MarketEventType.BOOK_DELTA,
    )

    assert gateway.accept(first) is True
    assert gateway.accept(second) is False
    assert gateway.queue._queue._queue[0] is first
    assert gateway.state is GatewayState.DEGRADED
    assert gateway.health.degraded is True



@pytest.mark.asyncio
async def test_gateway_trace_separates_source_to_ingest_from_post_ingest_age():
    now = datetime.now(timezone.utc)
    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=now - timedelta(seconds=4),
        ingest_time=now - timedelta(seconds=1),
        payload={"price": 100},
        source=MarketSource.WEBSOCKET,
    )
    gateway = MarketDataGateway("binance", "usd_m_futures", lambda _: None)
    gateway.begin_connect()
    gateway.mark_connected()

    with patch("aitos.forensics.data_flow_hooks.note_trace") as note_trace:
        assert gateway.accept(event) is True

    extra = note_trace.call_args.kwargs["extra"]
    assert extra["phase"] == "ingress"
    assert 3900 <= extra["event_age_ms"] <= 4100
    assert 2900 <= extra["source_to_ingest_ms"] <= 3100
    assert 900 <= extra["ingest_to_gateway_ms"] <= 1100
