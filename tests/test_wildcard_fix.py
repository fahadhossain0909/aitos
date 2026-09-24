import asyncio
import pytest

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus


@pytest.mark.asyncio
async def test_wildcard_subscription_matches_base_topics():
    """register_expected_topics(['market.kline']) should allow
    subscribe('market.kline.*') to create groups for 'market.kline.BTCUSDT.15m'
    events — NOT on the literal wildcard string.
    """
    from fakeredis.aioredis import FakeRedis

    r = FakeRedis()
    bus = EventBus(redis_client=r)
    await bus.initialize({})

    # Simulate what app.py does
    bus.register_expected_topics([
        "market.kline",
        "market.trade",
    ])

    received = []

    async def handler(event):
        received.append(event)

    # Subscribe with wildcard - should resolve base topics
    sub = await bus.subscribe("market.kline.*", handler, group="test-group")
    await asyncio.sleep(0.05)

    # Verify: NO group on literal wildcard
    try:
        groups = await r.xinfo_groups("stream:market.kline.*")
        assert False, "Bug still present: group exists on literal wildcard"
    except Exception as exc:
        assert "no such key" in str(exc).lower()

    # Publish a concrete event
    await bus.publish(
        Event(
            topic="market.kline.BTCUSDT.15m",
            payload={"close": 50000},
            source_module="test",
        )
    )

    # Wait for consume loop to discover and create group
    await asyncio.sleep(0.5)

    # Verify: Group now exists on concrete stream
    groups = await r.xinfo_groups("stream:market.kline.BTCUSDT.15m")
    assert len(groups) == 1
    gname = groups[0].get("name") or groups[0].get(b"name")
    assert gname in ("test-group", b"test-group")

    # Verify: Event was delivered
    assert len(received) == 1
    assert received[0].topic == "market.kline.BTCUSDT.15m"

    sub.cancel()


@pytest.mark.asyncio
async def test_wildcard_subscription_matches_concrete_topics():
    """fnmatch case: topics that already match via wildcard should still work.
    """
    from fakeredis.aioredis import FakeRedis

    r = FakeRedis()
    bus = EventBus(redis_client=r)
    await bus.initialize({})

    # Register a concrete topic
    bus.register_expected_topics(["market.kline.ETHUSDT.15m"])

    received = []

    async def handler(event):
        received.append(event)

    # Subscribe with wildcard
    sub = await bus.subscribe("market.kline.*", handler, group="test-group")
    await asyncio.sleep(0.05)

    # Publish the event
    await bus.publish(
        Event(
            topic="market.kline.ETHUSDT.15m",
            payload={"close": 3000},
            source_module="test",
        )
    )

    await asyncio.sleep(0.5)

    # Verify group on concrete stream
    groups = await r.xinfo_groups("stream:market.kline.ETHUSDT.15m")
    assert len(groups) == 1

    assert len(received) == 1
    sub.cancel()
