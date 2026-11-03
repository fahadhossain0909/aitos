from aitos.core.contracts import Event, EventPriority


def test_event_wire_roundtrip():
    event = Event(
        topic="intel.orderflow.BTCUSDT.1m",
        payload={"delta": 123.45, "cvd": -50, "symbol": "BTCUSDT"},
        source_module="orderflow-agent",
        priority=EventPriority.HIGH,
        correlation_id="corr-1",
    )
    wire = event.to_wire()
    restored = Event.from_wire(wire)

    assert restored.topic == event.topic
    assert restored.payload == event.payload
    assert restored.source_module == event.source_module
    assert restored.priority == EventPriority.HIGH
    assert restored.correlation_id == "corr-1"
    assert restored.event_id == event.event_id


def test_event_default_priority_is_normal():
    event = Event(topic="test.topic", payload={})
    assert event.priority == EventPriority.NORMAL
