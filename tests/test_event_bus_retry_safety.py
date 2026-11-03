import pytest

from aitos.eventbus.redis_bus import MAX_DELIVERY_ATTEMPTS, EventBus


class FakeRedis:
    def __init__(self):
        self.acks = []
        self.adds = []
        self.operations = []

    async def xack(self, stream, group, entry_id):
        self.operations.append("ack")
        self.acks.append((stream, group, entry_id))

    async def xadd(self, stream, fields, **kwargs):
        self.operations.append("add")
        self.adds.append((stream, fields, kwargs))
        return "new-id"


@pytest.mark.asyncio
async def test_failed_event_is_replaced_before_source_ack():
    redis = FakeRedis()
    bus = EventBus(redis)
    await bus._handle_failed_event(
        "stream:market.trade",
        "market-scanner",
        "1-0",
        {"topic": "market.trade"},
        RuntimeError("x"),
    )
    assert redis.operations == ["add", "ack"]
    assert redis.acks == [("stream:market.trade", "market-scanner", "1-0")]
    assert redis.adds[0][1]["_delivery_attempts"] == 1
    assert bus._retry_events == 1
    assert bus._dlq_events == 0


@pytest.mark.asyncio
async def test_terminal_failure_goes_to_dlq_and_clears_pel():
    redis = FakeRedis()
    bus = EventBus(redis)
    fields = {"topic": "market.trade", "_delivery_attempts": MAX_DELIVERY_ATTEMPTS - 1}
    await bus._handle_failed_event(
        "stream:market.trade",
        "market-scanner",
        "2-0",
        fields,
        RuntimeError("bad payload"),
    )
    assert redis.operations == ["add", "ack"]
    assert redis.acks == [("stream:market.trade", "market-scanner", "2-0")]
    assert redis.adds[0][0] == "stream:dlq"
    assert redis.adds[0][1]["_delivery_attempts"] == MAX_DELIVERY_ATTEMPTS
    assert bus._dlq_events == 1
