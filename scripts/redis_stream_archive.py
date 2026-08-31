#!/usr/bin/env python3
"""Crash-safe Redis Stream archival with durable per-stream file cursors."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

HOST = os.getenv("REDIS_HOST", "redis")
PORT = int(os.getenv("REDIS_PORT", "6379"))
ROOT = Path(os.getenv("REDIS_ARCHIVE_DIR", "/archive"))
CURSOR_FILE = ROOT / ".cursors.json"
POLL = max(0.1, float(os.getenv("REDIS_ARCHIVE_POLL_SECONDS", "1")))
RETRY = max(0.5, float(os.getenv("REDIS_ARCHIVE_RETRY_SECONDS", "2")))
BATCH = max(1, int(os.getenv("REDIS_ARCHIVE_BATCH_SIZE", "1000")))
DEFAULT_MAXLEN = max(1, int(os.getenv("REDIS_STREAM_MAXLEN_DEFAULT", "5000")))
STREAM_MAXLEN = {
    "stream:market.trade.": 25000,
    "stream:market.orderbook.": 25000,
    "stream:market.liquidity.": 100000,
    "stream:market.live_state.": 25000,
    "stream:market.orderflow.": 25000,
    "stream:market.kline.": 10000,
    "stream:market.opportunity_scanned": 5000,
    "stream:decision.": 10000,
    "stream:journal.": 10000,
    "stream:trade.": 10000,
    "stream:risk.": 10000,
    "stream:intel.": 10000,
    "stream:dlq": 25000,
}


def maxlen_for(key: str) -> int:
    for prefix, maxlen in STREAM_MAXLEN.items():
        if key.startswith(prefix):
            return maxlen
    return DEFAULT_MAXLEN


def safe_name(key: str) -> str:
    return key.removeprefix("stream:").replace("/", "_").replace("..", "_")


def decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def id_tuple(value: str) -> tuple[int, int]:
    millis, sequence = value.split("-", 1)
    return int(millis), int(sequence)


def id_lt(left: str, right: str) -> bool:
    return id_tuple(left) < id_tuple(right)


class ArchiveWriter:
    def __init__(self) -> None:
        self.root = ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        directory = self.root / safe_name(key)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "archive.jsonl"

    def append_and_checkpoint(
        self,
        key: str,
        entries: list[tuple[Any, dict[Any, Any]]],
        cursors: dict[str, dict[str, Any]],
    ) -> None:
        if not entries:
            return
        path = self._path(key)
        previous = cursors.get(key)
        if previous and previous.get("file") == str(path):
            offset = int(previous.get("offset", 0))
            with path.open("r+b") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size < offset:
                    raise RuntimeError(
                        f"archive file is shorter than checkpoint: {path}"
                    )
                if size > offset:
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
        with path.open("a", encoding="utf-8") as handle:
            for entry_id, fields in entries:
                record = {
                    "stream": key,
                    "stream_id": decode(entry_id),
                    "fields": {decode(k): decode(v) for k, v in fields.items()},
                }
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
            end_offset = handle.tell()
        cursors[key] = {
            "id": decode(entries[-1][0]),
            "file": str(path),
            "offset": end_offset,
        }
        self.save(cursors)

    def save(self, cursors: dict[str, dict[str, Any]]) -> None:
        tmp = CURSOR_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cursors, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CURSOR_FILE)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def recover(self, cursors: dict[str, Any]) -> dict[str, dict[str, Any]]:
        changed = False
        normalized = {}
        for key, value in cursors.items():
            if isinstance(value, dict) and {"id", "file", "offset"}.issubset(value):
                path = Path(value["file"])
                if path.exists():
                    with path.open("r+b") as handle:
                        handle.seek(0, os.SEEK_END)
                        size = handle.tell()
                        offset = int(value["offset"])
                        if size < offset:
                            raise RuntimeError(
                                f"archive file is shorter than checkpoint: {path}"
                            )
                        if size > offset:
                            handle.truncate(offset)
                            handle.flush()
                            os.fsync(handle.fileno())
                normalized[key] = value
                continue
            target = str(value)
            found = False
            directory = self.root / safe_name(key)
            for path in (
                sorted(directory.glob("*.jsonl")) if directory.exists() else []
            ):
                with path.open("rb") as handle:
                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        try:
                            record = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if str(record.get("stream_id")) == target:
                            normalized[key] = {
                                "id": target,
                                "file": str(path),
                                "offset": handle.tell(),
                            }
                            found = True
                            break
                if found:
                    break
            if found:
                changed = True
            else:
                normalized[key] = {
                    "id": target,
                    "file": "",
                    "offset": 0,
                    "legacy": True,
                }
        if changed:
            self.save(normalized)
        return normalized


async def archive_stream(
    r: redis.Redis, writer: ArchiveWriter, key: str, cursors: dict[str, dict[str, Any]]
) -> bool:
    state = cursors.get(key, {})
    cursor = str(state.get("id", "0-0"))
    response = await r.xread({key: cursor}, count=BATCH)
    if response:
        _, entries = response[0]
        writer.append_and_checkpoint(key, entries, cursors)
        return True
    if state.get("legacy"):
        return False
    maxlen = maxlen_for(key)
    boundary_rows = await r.xrevrange(key, max="+", min="-", count=maxlen + 1)
    if len(boundary_rows) <= maxlen:
        return False
    eviction_boundary = decode(boundary_rows[-1][0])
    if id_lt(cursor, eviction_boundary):
        return False
    await r.xtrim(key, maxlen=maxlen, approximate=True)
    return True


async def archive_forever(
    r: redis.Redis, writer: ArchiveWriter, cursors: dict[str, dict[str, Any]]
) -> None:
    while True:
        try:
            keys = []
            async for raw_key in r.scan_iter(match="stream:*", count=200):
                keys.append(decode(raw_key))
            for key in sorted(keys):
                for _ in range(20):
                    if not await archive_stream(r, writer, key, cursors):
                        break
            await asyncio.sleep(POLL)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            # Redis is allowed to restart independently. Keep the archive worker
            # alive and retry without losing the durable file cursor.
            print(
                f"Redis unavailable; retrying archive connection: {exc!r}", flush=True
            )
            await asyncio.sleep(RETRY)


async def main() -> None:
    writer = ArchiveWriter()
    cursors = writer.recover(writer.load())
    while True:
        r = redis.Redis(host=HOST, port=PORT, decode_responses=False)
        try:
            await r.ping()
            print("Redis archive connection established", flush=True)
            await archive_forever(r, writer, cursors)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            print(f"Redis unavailable; retrying: {exc!r}", flush=True)
            await asyncio.sleep(RETRY)
        finally:
            await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
