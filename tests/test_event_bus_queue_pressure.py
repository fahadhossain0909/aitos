import asyncio
from types import SimpleNamespace

import pytest

from aitos.eventbus.redis_bus import EventBus


class PendingRedis:
    def __init__(self, idle_ms=0):
        self.idle_ms = idle_ms
        self.calls = []

    async def xpending_range(self, stream, group, *, min, max, count):
        self.calls.append((stream, group, min, max, count))
        values = self.idle_ms if isinstance(self.idle_ms, list) else [self.idle_ms]
        return [{"time_since_delivered": value} for value in values[:count]]


@pytest.mark.asyncio
async def test_queue_lag_seconds_reports_oldest_pending_idle_age():
    redis = PendingRedis(idle_ms=12_500)
    bus = EventBus(redis)
    bus._known_topics.update({"market.trade.BTCUSDT", "market.book.delta.BTCUSDT"})
    bus._subscriptions = [
        SimpleNamespace(topic_pattern="market.trade.*", group="scanner"),
        SimpleNamespace(topic_pattern="market.book.*", group="book-cache"),
    ]

    lag = await bus.queue_lag_seconds()

    assert lag == 12.5
    assert len(redis.calls) == 2
    assert all(call[4] == 100 for call in redis.calls)


@pytest.mark.asyncio
async def test_queue_lag_seconds_returns_none_when_no_subscribed_topics():
    redis = PendingRedis()
    bus = EventBus(redis)
    bus._subscriptions = [
        SimpleNamespace(topic_pattern="market.trade.*", group="scanner")
    ]

    assert await bus.queue_lag_seconds() is None
    assert redis.calls == []


@pytest.mark.asyncio
async def test_queue_lag_seconds_keeps_unknown_when_pending_api_unavailable():
    class UnsupportedRedis:
        async def xpending_range(self, *args, **kwargs):
            raise NotImplementedError

    bus = EventBus(UnsupportedRedis())
    bus._known_topics.add("market.trade.BTCUSDT")
    bus._subscriptions = [
        SimpleNamespace(topic_pattern="market.trade.*", group="scanner")
    ]

    assert await bus.queue_lag_seconds() is None


@pytest.mark.asyncio
async def test_queue_lag_seconds_bounds_group_queries():
    redis = PendingRedis(idle_ms=250)
    bus = EventBus(redis)
    bus._known_topics.update(f"market.trade.{index}" for index in range(10))
    bus._subscriptions = [
        SimpleNamespace(topic_pattern="market.trade.*", group=f"group-{index}")
        for index in range(10)
    ]

    lag = await bus.queue_lag_seconds(max_groups=3)

    assert lag == 0.25
    assert len(redis.calls) == 3


def test_queue_lag_seconds_rejects_invalid_query_bound():
    bus = EventBus(PendingRedis())
    with pytest.raises(ValueError):
        asyncio.run(bus.queue_lag_seconds(max_groups=0))



@pytest.mark.asyncio
async def test_queue_lag_seconds_uses_max_idle_not_first_stream_id():
    # XPENDING is ordered by stream ID, which is not the same as idle age.
    redis = PendingRedis(idle_ms=[250, 18_000, 900])
    bus = EventBus(redis)
    bus._known_topics.add("market.trade.BTCUSDT")
    bus._subscriptions = [
        SimpleNamespace(topic_pattern="market.trade.*", group="scanner")
    ]

    assert await bus.queue_lag_seconds() == 18.0
    assert redis.calls[0][4] == 100
