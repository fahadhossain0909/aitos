"""Bounded multi-session AMT history and migration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .engine import AMTContext, ValueMigration


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    start: datetime
    end: datetime
    context: AMTContext


class SessionProfileStore:
    """Storage-neutral session history; ClickHouse can implement the same contract."""

    def __init__(self, max_sessions: int = 256) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be > 0")
        self.max_sessions = max_sessions
        self._items: dict[str, SessionSnapshot] = {}

    def upsert(self, snapshot: SessionSnapshot) -> None:
        self._items[snapshot.session_id] = snapshot
        if len(self._items) > self.max_sessions:
            oldest = min(self._items.values(), key=lambda x: x.start)
            self._items.pop(oldest.session_id, None)

    def get(self, session_id: str) -> SessionSnapshot | None:
        return self._items.get(session_id)

    def previous(self, session_id: str) -> SessionSnapshot | None:
        current = self._items.get(session_id)
        if current is None:
            return None
        candidates = [x for x in self._items.values() if x.start < current.start]
        return max(candidates, key=lambda x: x.start) if candidates else None

    def recent(self, limit: int = 20) -> Sequence[SessionSnapshot]:
        if limit <= 0:
            return ()
        return tuple(
            sorted(self._items.values(), key=lambda x: x.start, reverse=True)[:limit]
        )

    def migration(self, session_id: str) -> ValueMigration:
        current = self._items.get(session_id)
        prev = self.previous(session_id)
        if current is None or prev is None:
            return ValueMigration.UNKNOWN
        return current.context.value_migration
