"""Contract checks shared by all event transport implementations."""

from aitos.eventbus.contracts import EventBusProtocol, SubscriptionProtocol
from aitos.eventbus.redis_bus import EventBus, Subscription


def test_redis_streams_bus_satisfies_transport_protocol() -> None:
    # Runtime protocol checks verify the public structural surface without
    # requiring a Redis server or initializing a connection.
    assert isinstance(EventBus(redis_client=object()), EventBusProtocol)


def test_redis_subscription_satisfies_subscription_protocol() -> None:
    # The task is only inspected by the Redis adapter; callers depend on cancel.
    import asyncio

    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    subscription = Subscription(
        topic_pattern="market.trade",
        group="contract-test",
        consumer="contract-test-1",
        _task=task,
    )
    assert isinstance(subscription, SubscriptionProtocol)
    subscription.cancel()
