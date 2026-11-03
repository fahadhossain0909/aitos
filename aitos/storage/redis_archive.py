"""Durable append-only archive for Redis Stream events.

Redis is the hot event bus; this archive keeps historical events on the
persistent data disk so Redis can use bounded hot retention without silently
losing long-term history.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

_SAFE_TOPIC = re.compile(r"[^A-Za-z0-9_.-]+")


class RedisEventArchive:
    """Append Redis stream events to daily JSONL files on persistent storage."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.getenv("REDIS_ARCHIVE_DIR", "/archive"))
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def append(self, topic: str, event_id: str, fields: dict[str, Any]) -> None:
        """Append and fsync one event before it becomes eligible for hot eviction."""
        safe_topic = _SAFE_TOPIC.sub("_", topic).strip("._") or "unknown"
        now = datetime.now(timezone.utc)
        directory = self._root / safe_topic
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{now:%Y-%m-%d}.jsonl"
        record = {
            "archived_at": now.isoformat(),
            "stream_id": event_id,
            "topic": topic,
            "fields": fields,
        }
        line = json.dumps(
            record, ensure_ascii=False, default=str, separators=(",", ":")
        )
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
