"""Replay normalized order and account events from an archived JSONL stream."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LifecycleReplayEvent:
    event_id: str
    timestamp_ms: int
    event_type: str
    symbol: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_type or self.timestamp_ms < 0:
            raise ValueError(
                "event id, event type and non-negative timestamp are required"
            )
        if not isinstance(self.payload, dict):
            raise ValueError("lifecycle replay payload must be an object")


class LifecycleEventReplay:
    """Deterministic, idempotency-aware event projector; never submits orders."""

    def __init__(
        self,
        handler: Callable[[LifecycleReplayEvent], Any | Awaitable[Any]],
        *,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.handler = handler
        self._processed: set[str] = set()
        self.checkpoint_path = str(checkpoint_path) if checkpoint_path else None
        if self.checkpoint_path:
            Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.checkpoint_path) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS lifecycle_replay_events "
                    "(event_id TEXT PRIMARY KEY, timestamp_ms INTEGER NOT NULL)"
                )

    def _contains(self, event_id: str) -> bool:
        if self.checkpoint_path:
            with sqlite3.connect(self.checkpoint_path) as db:
                row = db.execute(
                    "SELECT 1 FROM lifecycle_replay_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            return row is not None
        return event_id in self._processed

    def _mark_processed(self, event: LifecycleReplayEvent) -> None:
        if self.checkpoint_path:
            with sqlite3.connect(self.checkpoint_path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO lifecycle_replay_events "
                    "(event_id,timestamp_ms) VALUES(?,?)",
                    (event.event_id, event.timestamp_ms),
                )
        else:
            self._processed.add(event.event_id)

    @staticmethod
    def read_jsonl(path: str | Path) -> list[LifecycleReplayEvent]:
        events = []
        for line_no, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                payload = raw.get("payload", raw)
                events.append(
                    LifecycleReplayEvent(
                        event_id=str(raw["event_id"]),
                        timestamp_ms=int(raw["timestamp_ms"]),
                        event_type=str(raw["event_type"]),
                        symbol=str(
                            raw.get("symbol", payload.get("symbol", ""))
                        ).upper(),
                        payload=dict(payload),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid lifecycle replay row {line_no}: {exc}"
                ) from exc
        return sorted(events, key=lambda item: (item.timestamp_ms, item.event_id))

    async def replay(self, events: Iterable[LifecycleReplayEvent]) -> dict[str, int]:
        ordered = sorted(events, key=lambda item: (item.timestamp_ms, item.event_id))
        processed = duplicates = 0
        for event in ordered:
            if self._contains(event.event_id):
                duplicates += 1
                continue
            result = self.handler(event)
            if hasattr(result, "__await__"):
                await result
            self._mark_processed(event)
            processed += 1
        return {"seen": len(ordered), "processed": processed, "duplicates": duplicates}
