"""Small, dependency-free health model for the market-data gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class GatewayHealth:
    """Observable lifecycle state for one market-data transport."""

    venue: str
    market_type: str
    connected: bool = False
    degraded: bool = False
    reconnect_count: int = 0
    decode_errors: int = 0
    sequence_errors: int = 0
    received_events: int = 0
    published_events: int = 0
    dropped_events: int = 0
    last_event_at: datetime | None = None
    last_error: str | None = None
    _updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record_event(self) -> None:
        self.received_events += 1
        self.last_event_at = datetime.now(timezone.utc)
        self._updated_at = self.last_event_at

    def record_publish(self) -> None:
        self.published_events += 1
        self._updated_at = datetime.now(timezone.utc)

    def record_error(self, stage: str, message: str) -> None:
        if stage == "decode":
            self.decode_errors += 1
        elif stage == "sequence":
            self.sequence_errors += 1
        self.last_error = message[:500]
        self.degraded = True
        self._updated_at = datetime.now(timezone.utc)

    def reconnect(self) -> None:
        self.reconnect_count += 1
        self.connected = False
        self.degraded = True
        self._updated_at = datetime.now(timezone.utc)

    def snapshot(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        source_age_ms = None
        if self.last_event_at is not None:
            source_age_ms = max(0, int((now - self.last_event_at).total_seconds() * 1000))
        return {
            "venue": self.venue,
            "market_type": self.market_type,
            "connected": self.connected,
            "degraded": self.degraded,
            "reconnect_count": self.reconnect_count,
            "decode_errors": self.decode_errors,
            "sequence_errors": self.sequence_errors,
            "received_events": self.received_events,
            "published_events": self.published_events,
            "dropped_events": self.dropped_events,
            "source_age_ms": source_age_ms,
            "last_error": self.last_error,
            "updated_at": self._updated_at.isoformat(),
        }
