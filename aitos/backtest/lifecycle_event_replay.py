"""Replay normalized order and account events from an archived JSONL stream."""

from __future__ import annotations

import json
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


class LifecycleEventReplay:
    """Deterministic, idempotency-aware event projector; never submits orders."""

    def __init__(
        self,
        handler: Callable[[LifecycleReplayEvent], Any | Awaitable[Any]],
    ) -> None:
        self.handler = handler
        self._processed: set[str] = set()

    @staticmethod
    def read_jsonl(path: str | Path) -> list[LifecycleReplayEvent]:
        events = []
        for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
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
                        symbol=str(raw.get("symbol", payload.get("symbol", ""))).upper(),
                        payload=dict(payload),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid lifecycle replay row {line_no}: {exc}") from exc
        return sorted(events, key=lambda item: (item.timestamp_ms, item.event_id))

    async def replay(self, events: Iterable[LifecycleReplayEvent]) -> dict[str, int]:
        ordered = sorted(events, key=lambda item: (item.timestamp_ms, item.event_id))
        processed = duplicates = 0
        for event in ordered:
            if event.event_id in self._processed:
                duplicates += 1
                continue
            result = self.handler(event)
            if hasattr(result, "__await__"):
                await result
            self._processed.add(event.event_id)
            processed += 1
        return {"seen": len(ordered), "processed": processed, "duplicates": duplicates}
