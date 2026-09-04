"""Trading-session and macro-event abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class MarketSession:
    name: str
    opens_at: datetime
    closes_at: datetime

    def contains(self, when: datetime) -> bool:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return self.opens_at <= when < self.closes_at


@dataclass(frozen=True, slots=True)
class MacroEvent:
    event_id: str
    name: str
    scheduled_at: datetime
    importance: str = "medium"
    currency: str | None = None

    def seconds_to(self, when: datetime) -> float:
        return (self.scheduled_at - when).total_seconds()


class TradingCalendar:
    """Explicit session/event boundary; 24/7 is the safe default."""

    def __init__(self, sessions: tuple[MarketSession, ...] = (), events: tuple[MacroEvent, ...] = ()) -> None:
        self.sessions = sessions
        self.events = events

    def is_open(self, when: datetime) -> bool:
        return not self.sessions or any(session.contains(when) for session in self.sessions)

    def upcoming(self, when: datetime, horizon: timedelta = timedelta(hours=24)) -> tuple[MacroEvent, ...]:
        end = when + horizon
        return tuple(event for event in self.events if when <= event.scheduled_at <= end)
