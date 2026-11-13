"""Bridge normalized intelligence messages onto the existing AITOS EventBus."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aitos.core.contracts import Event
from aitos.eventbus.contracts import EventBusProtocol


class EventBusSink:
    def __init__(self, event_bus: EventBusProtocol, *, source_module: str = "world-intelligence"):
        self.event_bus = event_bus
        self.source_module = source_module

    async def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        event_id = str(payload.get("event_id") or "")
        await self.event_bus.publish(Event(
            topic=topic,
            payload=dict(payload),
            event_id=event_id or None,  # Event contract generates an ID when omitted.
            source_module=self.source_module,
            schema_version=str(payload.get("schema_version", "1")),
        ))


__all__ = ["EventBusSink"]
