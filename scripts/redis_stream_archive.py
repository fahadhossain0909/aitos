#!/usr/bin/env python3
"""Archive Redis Streams to the persistent data disk, then bound hot memory."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
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

# Hot retention protects RAM. Full history is written to the data disk before
# this worker trims entries from Redis.
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


def _decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class ArchiveWriter:
    def __init__(self) -> None:
        self.root = ARCHIVE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def append_batch(
        self, key: str, entries: list[tuple[Any, dict[Any, Any]]]
    ) -> None:
        if not entries:
            return
        topic_dir = self.root / _safe_name(key)
        topic_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = topic_dir / f"{date}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for entry_id, fields in entries:
                normalized = {_decode(k): _decode(v) for k, v in fields.items()}
                record = {"stream_id": _decode(entry_id), "fields": normalized}
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
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


async def archive_stream(
    r: redis.Redis,
    writer: ArchiveWriter,
    key: str,
    cursors: dict[str, str],
) -> bool:
    """Archive one bounded batch and trim only after the safe boundary is archived."""
    cursor = cursors.get(key, "0-0")
    response = await r.xread({key: cursor}, count=BATCH_SIZE)
    if response:
        _, entries = response[0]
        writer.append_batch(key, entries)
        cursors[key] = _decode(entries[-1][0])
        writer.save_cursors(cursors)
        return True

    maxlen = maxlen_for(key)
    boundary = await r.xrevrange(key, max="+", min="-", count=maxlen + 1)
    if len(boundary) <= maxlen:
        return False

    # The oldest item in this newest-(maxlen+1) window is the first item that
    # may be evicted. We only trim after the archive cursor reaches it.
    eviction_boundary = _decode(boundary[-1][0])
    if cursor < eviction_boundary:
        return False

    await r.xtrim(key, maxlen=maxlen, approximate=True)
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
                keys.append(_decode(raw_key))

            for key in sorted(keys):
                # Drain a bounded number of batches per scan so one hot stream
                # cannot starve the others. Never trim until its archive cursor
                # has crossed the eviction boundary.
                for _ in range(20):
                    progressed = await archive_stream(r, writer, key, cursors)
                    if not progressed:
                        break
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
