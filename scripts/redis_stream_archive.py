#!/usr/bin/env python3
"""Archive Redis Streams to the persistent data disk, then bound hot memory."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import redis.asyncio as redis


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
ARCHIVE_ROOT = Path(os.getenv("REDIS_ARCHIVE_DIR", "/archive"))
CURSOR_FILE = ARCHIVE_ROOT / ".cursors.json"
POLL_SECONDS = float(os.getenv("REDIS_ARCHIVE_POLL_SECONDS", "1"))
BATCH_SIZE = int(os.getenv("REDIS_ARCHIVE_BATCH_SIZE", "1000"))
DEFAULT_MAXLEN = int(os.getenv("REDIS_STREAM_MAXLEN_DEFAULT", "5000"))

# Hot retention is intentionally small enough to protect RAM, while the full
# history is preserved on the data disk before trimming.
STREAM_MAXLEN = {
    "stream:market.trade.": 25_000,
    "stream:market.orderbook.": 25_000,
    "stream:market.liquidity.": 100_000,
    "stream:market.live_state.": 25_000,
    "stream:market.orderflow.": 25_000,
    "stream:market.kline.": 10_000,
    "stream:market.opportunity_scanned": 5_000,
    "stream:decision.": 10_000,
    "stream:journal.": 10_000,
    "stream:trade.": 10_000,
    "stream:risk.": 10_000,
    "stream:intel.": 10_000,
    "stream:dlq": 25_000,
}


def maxlen_for(key: str) -> int:
    for prefix, maxlen in STREAM_MAXLEN.items():
        if key.startswith(prefix):
            return maxlen
    return DEFAULT_MAXLEN


def _safe_name(key: str) -> str:
    return key.removeprefix("stream:").replace("/", "_").replace("..", "_")


class ArchiveWriter:
    def __init__(self) -> None:
        self.root = ARCHIVE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def append_batch(self, key: str, entries: list[tuple[Any, dict[Any, Any]]]) -> None:
        if not entries:
            return
        topic_dir = self.root / _safe_name(key)
        topic_dir.mkdir(parents=True, exist_ok=True)
        # Redis stream IDs contain the Redis-generated timestamp; use the
        # archive host's UTC date only for file rotation.
        from datetime import datetime, timezone

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = topic_dir / f"{date}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for entry_id, fields in entries:
                normalized = {
                    (k.decode() if isinstance(k, bytes) else str(k)):
                    : (v.decode() if isinstance(v, bytes) else v)
                    for k, v in fields.items()
                }
                record = {
                    "stream_id": entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id),
                    "fields": normalized,
                }
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def save_cursors(self, cursors: dict[str, str]) -> None:
        tmp = CURSOR_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursors, sort_keys=True), encoding="utf-8")
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp.replace(CURSOR_FILE)

    def load_cursors(self) -> dict[str, str]:
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


async def ensure_archive_cursor(r: redis.Redis, key: str, cursor: str) -> str:
    """Detect a cursor that fell behind the hot stream and resume safely."""
    first = await r.xrange(key, min="-", max="+", count=1)
    if not first:
        return cursor
    first_id = first[0][0]
    first_id = first_id.decode() if isinstance(first_id, bytes) else str(first_id)
    # If the archive cursor is older than the oldest hot entry, those entries
    # may already have been evicted before this worker saw them. Start at the
    # oldest surviving entry; future entries remain lossless.
    if cursor != "0-0" and cursor < first_id:
        return f"{first_id.split('-')[0]}-0"
    return cursor


async def archive_stream(r: redis.Redis, writer: ArchiveWriter, key: str, cursors: dict[str, str]) -> bool:
    cursor = await ensure_archive_cursor(r, key, cursors.get(key, "0-0"))
    response = await r.xread({key: cursor}, count=BATCH_SIZE, block=100)
    if not response:
        return False
    _, entries = response[0]
    writer.append_batch(key, entries)
    last_id = entries[-1][0]
    last_id = last_id.decode() if isinstance(last_id, bytes) else str(last_id)
    cursors[key] = last_id
    writer.save_cursors(cursors)

    # Only trim after the archive batch has been fsynced to the data disk.
    await r.xtrim(key, maxlen=maxlen_for(key), approximate=True)
    return True


async def main() -> None:
    writer = ArchiveWriter()
    cursors = writer.load_cursors()
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    try:
        await r.ping()
        while True:
            keys: list[str] = []
            async for raw_key in r.scan_iter(match="stream:*", count=200):
                key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
                keys.append(key)
            if keys:
                for key in sorted(keys):
                    while await archive_stream(r, writer, key, cursors):
                        # Drain backlog in bounded batches so trimming never
                        # races ahead of disk archival.
                        pass
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
