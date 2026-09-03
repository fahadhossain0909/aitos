"""Bounded, per-stream concurrency for Redis Stream consumers."""

from __future__ import annotations

import asyncio
import fnmatch
import os
from typing import Any

from aitos.logging_setup import get_logger

from .redis_bus import (
    CONSUMER_BATCH_SIZE,
    CONSUMER_BLOCK_MS,
    POLL_INTERVAL_SECONDS,
)

logger = get_logger("aitos.eventbus")

DEFAULT_CONSUMER_CONCURRENCY = 8
MAX_CONSUMER_CONCURRENCY = 32


def consumer_concurrency() -> int:
    raw = os.getenv("REDIS_CONSUMER_CONCURRENCY")
    if raw is None:
        return DEFAULT_CONSUMER_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONSUMER_CONCURRENCY
    return max(1, min(value, MAX_CONSUMER_CONCURRENCY))


def install_eventbus_consumer_concurrency(event_bus_cls: type[Any]) -> None:
    """Install ordered per-stream consumption on EventBus.

    A Redis Stream is an ordering boundary: entries from one stream are never
    processed concurrently. Independent streams may process concurrently up to
    ``REDIS_CONSUMER_CONCURRENCY``. This avoids allowing a slow BTC handler to
    serialize ETH/SOL/BNB while preserving state-update ordering per symbol.
    """
    if getattr(event_bus_cls, "_ordered_concurrency_installed", False):
        return

    async def _consume_loop(
        self: Any,
        topic_pattern: str,
        group: str,
        consumer: str,
        handler: Any,
        *,
        live_only: bool = False,
        start_id: str = "0",
    ) -> None:
        streams_seen: set[str] = set()
        stream_tasks: dict[str, asyncio.Task] = {}
        semaphore = asyncio.Semaphore(consumer_concurrency())

        async def consume_stream(stream_topic: str) -> None:
            stream_key = f"stream:{stream_topic}"
            try:
                if not live_only:
                    while True:
                        pending = await self._reclaim_pending(
                            stream_key, group, consumer
                        )
                        if not pending:
                            break
                        for entry_id, fields in pending:
                            async with semaphore:
                                await self._process_message(
                                    stream_key, entry_id, fields, group, handler
                                )

                while True:
                    try:
                        resp = await self._redis.xreadgroup(
                            groupname=group,
                            consumername=consumer,
                            streams={stream_key: ">"},
                            count=CONSUMER_BATCH_SIZE,
                            block=CONSUMER_BLOCK_MS,
                        )
                    except Exception as exc:
                        logger.error(
                            "xreadgroup error",
                            extra={
                                "aitos_extra": {
                                    "stream": stream_key,
                                    "group": group,
                                    "error": str(exc),
                                }
                            },
                        )
                        await asyncio.sleep(1.0)
                        continue
                    if not resp:
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        continue
                    for returned_stream, messages in resp:
                        if isinstance(returned_stream, bytes):
                            returned_stream = returned_stream.decode()
                        for entry_id, fields in messages:
                            async with semaphore:
                                await self._process_message(
                                    returned_stream,
                                    entry_id,
                                    fields,
                                    group,
                                    handler,
                                )
            except asyncio.CancelledError:
                raise

        try:
            while True:
                if "*" in topic_pattern:
                    matching = {
                        t
                        for t in self._known_topics
                        if fnmatch.fnmatch(t, topic_pattern)
                    }
                    new_streams = matching - streams_seen
                    for stream_topic in sorted(new_streams):
                        await self._ensure_group(
                            f"stream:{stream_topic}",
                            group,
                            start_id=start_id,
                            reset_existing=live_only,
                        )
                    streams_seen |= matching
                else:
                    streams_seen = {topic_pattern}
                    await self._ensure_group(
                        f"stream:{topic_pattern}",
                        group,
                        start_id=start_id,
                        reset_existing=live_only,
                    )

                for stream_topic in sorted(streams_seen):
                    task = stream_tasks.get(stream_topic)
                    if task is None or task.done():
                        stream_tasks[stream_topic] = asyncio.create_task(
                            consume_stream(stream_topic)
                        )
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            for task in stream_tasks.values():
                task.cancel()
            if stream_tasks:
                await asyncio.gather(*stream_tasks.values(), return_exceptions=True)
            raise

    event_bus_cls._consume_loop = _consume_loop
    event_bus_cls._ordered_concurrency_installed = True
