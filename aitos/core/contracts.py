"""Core module & event contracts shared across all of AITOS.

Every subsystem (Event Bus, AI Kernel, Agents, future Risk/Data/Learning
modules) implements ``AITOSModule``. Every message that crosses a module
boundary is an ``Event``. These contracts intentionally have zero
dependency on infrastructure (no Redis, no DB) so they can be imported by
any layer without creating coupling.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventPriority(str, Enum):
    """Delivery priority hint for the Event Bus."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"  # e.g. risk breaches, emergency stop


class ModuleStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Event:
    """A single unit of communication on the Event Bus.

    Topics follow a dotted hierarchy, mirroring the spec, e.g.:
    ``intel.orderflow.BTCUSDT.1m`` or ``risk.circuit_breaker.triggered``.
    """

    topic: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=_new_id)
    source_module: str = "unknown"
    priority: EventPriority = EventPriority.NORMAL
    created_at: str = field(default_factory=_utc_now_iso)
    correlation_id: str | None = None
    schema_version: str = "1.0"

    def to_wire(self) -> dict[str, Any]:
        """Serialize to a flat dict suitable for Redis Streams (str values)."""
        import json

        return {
            "topic": self.topic,
            "payload": json.dumps(self.payload, default=str),
            "event_id": self.event_id,
            "source_module": self.source_module,
            "priority": self.priority.value,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id or "",
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_wire(cls, raw: dict[Any, Any]) -> Event:
        import json

        def _s(v: Any) -> str:
            return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

        # Redis clients configured with decode_responses=False return both
        # keys and values as bytes — normalize keys to str first so lookups
        # below work regardless of client decoding settings.
        raw = {_s(k): v for k, v in raw.items()}

        payload_raw = raw.get("payload", "{}")
        payload = json.loads(_s(payload_raw)) if payload_raw else {}
        correlation_id = _s(raw.get("correlation_id", "")) or None
        return cls(
            topic=_s(raw["topic"]),
            payload=payload,
            event_id=_s(raw.get("event_id", _new_id())),
            source_module=_s(raw.get("source_module", "unknown")),
            priority=EventPriority(_s(raw.get("priority", "normal"))),
            created_at=_s(raw.get("created_at", _utc_now_iso())),
            correlation_id=correlation_id,
            schema_version=_s(raw.get("schema_version", "1.0")),
        )


@dataclass(frozen=True)
class EventResponse:
    """Optional reply to an event, used for request/reply semantics."""

    request_event_id: str
    responder_module: str
    payload: dict[str, Any]
    success: bool = True
    error: str | None = None
    created_at: str = field(default_factory=_utc_now_iso)


@dataclass(frozen=True)
class HealthStatus:
    module_id: str
    status: ModuleStatus
    latency_ms: float
    last_event_time: str | None
    details: dict[str, Any] = field(default_factory=dict)


class AITOSModule(ABC):
    """Base contract every AITOS module (kernel, bus, agent, ...) must satisfy."""

    @property
    @abstractmethod
    def module_id(self) -> str:
        """Unique module identifier."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Semantic version string."""

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """One-time setup. Must be idempotent."""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Return current health status."""

    @abstractmethod
    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        """Graceful shutdown. Cleanup resources."""

    @abstractmethod
    async def emit_events(self) -> AsyncIterator[Event]:
        """Yield events this module produces (for modules that generate their own stream)."""

    @abstractmethod
    async def handle_event(self, event: Event) -> EventResponse | None:
        """Process an incoming event. Return a response if applicable."""
