from datetime import datetime, timezone

import pytest

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus
from aitos.market_data.bus import MarketDataBus
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource


class _Pipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    def xadd(self, stream, fields, **kwargs):
        self.commands.append((stream, fields, kwargs))
        return self

    async def execute(self, raise_on_error=True):
        self.client.pipeline_commands = list(self.commands)
        return [
            RuntimeError("simulated XADD failure")
            if index in self.client.fail_indices
            else f"{index + 1}-0"
            for index, _ in enumerate(self.commands)
        ]


class _Redis:
    def __init__(self, fail_indices=()):
        self.fail_indices = set(fail_indices)
        self.pipeline_commands = []
        self.single_xadds = []

    async def ping(self):
        return True

    def pipeline(self, transaction=False):
        assert transaction is False
        return _Pipeline(self)

    async def xadd(self, stream, fields, **kwargs):
        self.single_xadds.append((stream, fields, kwargs))
        return f"fallback-{len(self.single_xadds)}"


def _market_event(symbol: str, event_id: str) -> MarketEvent:
    now = datetime.now(timezone.utc)
    return MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="usd_m_futures",
        symbol=symbol,
        event_time=now,
        ingest_time=now,
        source=MarketSource.WEBSOCKET,
        event_id=event_id,
        payload={"symbol": symbol, "price": 100.0},
    )


@pytest.mark.asyncio
async def test_event_bus_publish_many_uses_one_redis_pipeline():
    redis = _Redis()
    bus = EventBus(redis)
    await bus.initialize({})
    events = [
        Event(topic="market.ticker", payload={"symbol": "BTCUSDT"}),
        Event(topic="market.ticker", payload={"symbol": "ETHUSDT"}),
    ]

    await bus.publish_many(events)

    assert len(redis.pipeline_commands) == 2
    assert redis.single_xadds == []
    assert bus._published_events == 2


@pytest.mark.asyncio
async def test_event_bus_retries_only_failed_pipeline_commands():
    redis = _Redis(fail_indices={1})
    bus = EventBus(redis)
    await bus.initialize({})
    events = [
        Event(topic="market.ticker", payload={"symbol": "BTCUSDT"}),
        Event(topic="market.ticker", payload={"symbol": "ETHUSDT"}),
    ]

    await bus.publish_many(events)

    assert len(redis.pipeline_commands) == 2
    assert len(redis.single_xadds) == 1
    assert redis.single_xadds[0][1]["event_id"] == events[1].event_id
    assert bus._published_events == 2


@pytest.mark.asyncio
async def test_market_data_bus_batch_preserves_market_event_identity():
    class FakeEventBus:
        def __init__(self):
            self.batch = []
            self.single = []

        async def publish_many(self, events):
            self.batch = list(events)

        async def publish(self, event):
            self.single.append(event)

    transport = FakeEventBus()
    market_bus = MarketDataBus(transport)
    events = [
        _market_event("BTCUSDT", "event-btc"),
        _market_event("ETHUSDT", "event-eth"),
    ]

    await market_bus.publish_many(events)

    assert transport.single == []
    assert [event.event_id for event in transport.batch] == [
        "event-btc",
        "event-eth",
    ]
    assert [event.topic for event in transport.batch] == [
        "market.ticker",
        "market.ticker",
    ]
