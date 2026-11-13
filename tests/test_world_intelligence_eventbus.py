import pytest

from aitos.core.contracts import Event
from aitos.world_intelligence.eventbus_adapter import EventBusSink


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event, priority=None):
        self.events.append(event)


@pytest.mark.asyncio
async def test_eventbus_sink_preserves_deterministic_event_identity():
    bus = Bus()
    sink = EventBusSink(bus)
    await sink.publish(
        "intelligence.events.normalized",
        {"event_id": "stable-event-id", "schema_version": 1, "payload": {"x": 1}},
    )
    event = bus.events[0]
    assert isinstance(event, Event)
    assert event.event_id == "stable-event-id"
    assert event.topic == "intelligence.events.normalized"
    assert event.source_module == "world-intelligence"
    assert event.payload["payload"] == {"x": 1}
