"""Small, dependency-free health model for the market-data gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class GatewayHealth:
    """Observable lifecycle and handoff state for one market-data transport."""

    venue: str
    market_type: str
    connected: bool = False
    degraded: bool = False
    reconnect_count: int = 0
    decode_errors: int = 0
    sequence_errors: int = 0
    received_events: int = 0
    accepted_events: int = 0
    rejected_events: int = 0
    published_events: int = 0
    publish_errors: int = 0
    dropped_events: int = 0
    stale_events: int = 0
    last_event_at: datetime | None = None
    last_accepted_at: datetime | None = None
    last_published_at: datetime | None = None
    last_error: str | None = None
    _updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record_event(self) -> None:
        self.received_events += 1
        self.last_event_at = datetime.now(timezone.utc)
        self._updated_at = self.last_event_at

    def record_accept(self) -> None:
        self.accepted_events += 1
        self.last_accepted_at = datetime.now(timezone.utc)
        self._updated_at = self.last_accepted_at

    def record_reject(self, stage: str, message: str) -> None:
        self.rejected_events += 1
        if stage == "stale_websocket":
            self.stale_events += 1
        self.record_error(stage, message)

    def record_publish(self) -> None:
        self.published_events += 1
        self.last_published_at = datetime.now(timezone.utc)
        self._updated_at = self.last_published_at

    def record_publish_error(self, message: str) -> None:
        self.publish_errors += 1
        self.record_error("publish", message)

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

    @staticmethod
    def _age_ms(now: datetime, timestamp: datetime | None) -> int | None:
        if timestamp is None:
            return None
        return max(0, int((now - timestamp).total_seconds() * 1000))

    def snapshot(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        return {
            "venue": self.venue,
            "market_type": self.market_type,
            "connected": self.connected,
            "degraded": self.degraded,
            "reconnect_count": self.reconnect_count,
            "decode_errors": self.decode_errors,
            "sequence_errors": self.sequence_errors,
            "received_events": self.received_events,
            "accepted_events": self.accepted_events,
            "rejected_events": self.rejected_events,
            "published_events": self.published_events,
            "publish_errors": self.publish_errors,
            "dropped_events": self.dropped_events,
            "stale_events": self.stale_events,
            "receive_to_now_age_ms": self._age_ms(now, self.last_event_at),
            "accept_to_now_age_ms": self._age_ms(now, self.last_accepted_at),
            "publish_to_now_age_ms": self._age_ms(now, self.last_published_at),
            "last_error": self.last_error,
            "updated_at": self._updated_at.isoformat(),
        }
