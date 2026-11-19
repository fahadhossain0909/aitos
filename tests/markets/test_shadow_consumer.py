from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import (
    MarketEvent as CanonicalMarketEvent,
    MarketEventType,
    MarketSource,
)
from aitos.markets.contracts import AssetClass, Instrument
from aitos.markets.engine import CrossMarketIntelligenceEngine
from aitos.markets.shadow_consumer import MarketDataShadowConsumer


def make_event(
    *,
    symbol: str = "BTCUSDT",
    market_type: str = "spot",
    event_id: str = "event-1",
    event_time: datetime | None = None,
    price: float = 100.0,
) -> CanonicalMarketEvent:
    now = event_time or datetime.now(timezone.utc)
    return CanonicalMarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        venue="binance",
        market="spot" if market_type == "spot" else "futures",
        market_type=market_type,
        symbol=symbol,
        instrument_id=f"{market_type}:{symbol}",
        event_time=now,
        ingest_time=now,
        payload={"lastPrice": str(price)},
        source=MarketSource.WEBSOCKET,
        event_id=event_id,
    )


def make_consumer(*, queue_capacity: int = 2) -> MarketDataShadowConsumer:
    return MarketDataShadowConsumer(
        engine=CrossMarketIntelligenceEngine(),
        instrument_resolver=lambda event: Instrument(
            event.symbol,
            AssetClass.CRYPTO,
            "binance",
            market_type=event.market_type,
        ),
        market_types=("spot", "usd_m_futures"),
        queue_capacity=queue_capacity,
        max_event_age_seconds=30,
    )


@pytest.mark.asyncio
async def test_shadow_consumer_uses_live_only_allowlisted_subscriptions():
    class Subscription:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    class Bus:
        def __init__(self):
            self.calls = []
            self.subscriptions = []

        async def subscribe(self, event_type, handler, **kwargs):
            self.calls.append((event_type, handler, kwargs))
            subscription = Subscription()
            self.subscriptions.append(subscription)
            return subscription

    bus = Bus()
    consumer = make_consumer()
    await consumer.start(bus)

    assert len(bus.calls) == 2
    assert {call[2]["market_type"] for call in bus.calls} == {
        "spot",
        "usd_m_futures",
    }
    assert all(call[2]["live_only"] for call in bus.calls)
    assert all("cross-market-shadow-v1-" in call[2]["group"] for call in bus.calls)

    await consumer.stop()
    assert all(subscription.cancelled for subscription in bus.subscriptions)
    assert not consumer.snapshot().started


@pytest.mark.asyncio
async def test_shadow_consumer_projects_events_and_deduplicates():
    consumer = make_consumer()
    event = make_event()
    await consumer._on_event(event)
    await consumer._on_event(event)
    await consumer._queue.join()

    snapshot = consumer.snapshot()
    instrument_id = "binance:crypto:spot:BTCUSDT"
    assert snapshot.accepted == 2
    assert snapshot.processed == 1
    assert snapshot.duplicate_events == 1
    assert consumer._engine.snapshot_series()[instrument_id] == (100.0,)


@pytest.mark.asyncio
async def test_shadow_consumer_drops_new_events_when_bounded_queue_is_full():
    consumer = make_consumer(queue_capacity=1)
    await consumer._on_event(make_event(event_id="first"))
    await consumer._on_event(make_event(event_id="second"))

    snapshot = consumer.snapshot()
    assert snapshot.queued == 1
    assert snapshot.accepted == 1
    assert snapshot.rejected_overload == 1


@pytest.mark.asyncio
async def test_shadow_consumer_rejects_stale_events():
    consumer = make_consumer()
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    await consumer._on_event(make_event(event_time=old))

    assert consumer.snapshot().rejected_stale == 1
    assert consumer.snapshot().queued == 0


def test_shadow_consumer_rejects_unresolved_and_out_of_order_events():
    consumer = make_consumer()
    consumer._resolver = lambda event: None
    consumer._process(make_event(event_id="unresolved"))
    assert consumer.snapshot().rejected_unresolved == 1

    consumer._resolver = lambda event: Instrument(
        event.symbol, AssetClass.CRYPTO, "binance", market_type=event.market_type
    )
    first = datetime.now(timezone.utc)
    consumer._process(make_event(event_id="newer", event_time=first))
    consumer._process(
        make_event(
            event_id="older",
            event_time=first - timedelta(milliseconds=1),
            price=99,
        )
    )
    assert consumer.snapshot().processed == 1
    assert consumer.snapshot().rejected_out_of_order == 1


def test_shadow_consumer_rejects_invalid_price_and_bounds_instrument_count():
    consumer = make_consumer()
    invalid = make_event(event_id="invalid")
    invalid.payload["lastPrice"] = "NaN"
    consumer._process(invalid)
    assert consumer.snapshot().rejected_invalid_price == 1

    limited = MarketDataShadowConsumer(
        engine=CrossMarketIntelligenceEngine(),
        instrument_resolver=lambda event: Instrument(
            event.symbol, AssetClass.CRYPTO, "binance", market_type=event.market_type
        ),
        market_types=("spot",),
        max_instruments=1,
    )
    limited._process(make_event(symbol="BTCUSDT", event_id="btc"))
    limited._process(make_event(symbol="ETHUSDT", event_id="eth"))
    assert limited.snapshot().processed == 1
    assert limited.snapshot().rejected_unresolved == 1
