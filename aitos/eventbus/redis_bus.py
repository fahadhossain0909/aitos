"""Event Bus — decoupled, ordered, at-least-once event transport.

Backed by Redis Streams:
- Each topic maps to one Redis Stream key (``stream:{topic}``).
- Subscribers join a consumer group per (topic, handler) so multiple
  instances of a module can share load while still getting at-least-once
  delivery with explicit ACKs.
- Messages that fail repeatedly are moved to a dead-letter stream
  (``stream:dlq``) instead of being retried forever.
- ``request_reply`` implements a lightweight RPC pattern on top of pub/sub
  using a private reply topic + Redis Pub/Sub for low latency.

This is designed to run against a real Redis instance (see
``docker-compose.yml``). For unit tests, pass in a ``fakeredis.aioredis``
client — the interface is identical.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import EventSchemaValidationError, ModuleNotInitializedError
from aitos.logging_setup import get_logger

logger = get_logger("aitos.eventbus")

EventHandler = Callable[[Event], Awaitable[EventResponse | None]]

DLQ_STREAM = "stream:dlq"
MAX_DELIVERY_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 0.15
CONSUMER_BLOCK_MS = 100
CONSUMER_BATCH_SIZE = 100
PENDING_RECLAIM_IDLE_MS = 5_000
PENDING_RECLAIM_BATCH_SIZE = 100

# Canonical market-data channels have bounded retention. Legacy prefixes are
# retained temporarily so existing non-v1 consumers remain safe while the
# market-data migration is completed.
STREAM_MAXLEN_DEFAULTS = {
    "market.trade": 25_000,
    "market.book.delta": 25_000,
    "market.book.snapshot": 5_000,
    "market.ticker": 10_000,
    "market.funding": 5_000,
    "market.open_interest": 10_000,
    "market.liquidation": 25_000,
    "market.options": 10_000,
    "market.instrument": 2_000,
    "market.trade.": 25_000,
    "market.orderbook.": 25_000,
    "market.liquidity.": 100_000,
    "market.live_state.": 25_000,
}


def _stream_key(topic: str) -> str:
    return f"stream:{topic}"


def _stream_maxlen(topic: str) -> int | None:
    for prefix, default in sorted(
        STREAM_MAXLEN_DEFAULTS.items(), key=lambda item: -len(item[0])
    ):
        if (
            topic == prefix
            or topic.startswith(prefix + ".")
            or topic.startswith(prefix)
        ):
            env_name = "REDIS_STREAM_MAXLEN_" + prefix.replace(".", "_").upper().rstrip(
                "_"
            )
            raw_value = os.getenv(env_name)
            if raw_value is None:
                return default
            try:
                value = int(raw_value)
            except ValueError:
                logger.warning(
                    "Invalid %s=%r; using default %d", env_name, raw_value, default
                )
                return default
            if value < 1:
                logger.warning(
                    "Invalid %s=%r; using default %d", env_name, raw_value, default
                )
                return default
            return value
    return None


def validate_event_schema(event: Event) -> None:
    if not event.topic or not isinstance(event.topic, str):
        raise EventSchemaValidationError("Event.topic must be a non-empty string")
    if not isinstance(event.payload, dict):
        raise EventSchemaValidationError("Event.payload must be a dict")


@dataclass
class Subscription:
    topic_pattern: str
    group: str
    consumer: str
    _task: asyncio.Task

    def cancel(self) -> None:
        self._task.cancel()


class EventBus(AITOSModule):
    """Redis Streams backed implementation of the AITOS Event Bus contract."""

    def __init__(self, redis_client: Any, module_id: str = "event-bus") -> None:
        self._redis = redis_client
        self._module_id = module_id
        self._initialized = False
        self._started_at: float | None = None
        self._last_event_time: str | None = None
        self._known_topics: set[str] = set()
        self._ensured_groups: set[tuple[str, str]] = set()
        self._subscriptions: list[Subscription] = []
        self._pending_replies: dict[str, asyncio.Future] = {}

    @property
    def module_id(self) -> str:
        return self._module_id

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        await self._redis.ping()
        self._initialized = True
        self._started_at = time.monotonic()
        logger.info("EventBus initialized")

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            await self._redis.ping()
            latency_ms = (time.monotonic() - start) * 1000
            status = ModuleStatus.HEALTHY
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            status = ModuleStatus.UNHEALTHY
            logger.error("EventBus health check failed: %s", exc)
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=latency_ms,
            last_event_time=self._last_event_time,
            details={"known_topics": sorted(self._known_topics)},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        if self._subscriptions:
            await asyncio.wait(
                [
                    asyncio.ensure_future(_await_cancelled(s._task))
                    for s in self._subscriptions
                ],
                timeout=grace_period_seconds,
            )
        self._subscriptions.clear()
        logger.info("EventBus shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def publish(
        self, event: Event, priority: EventPriority | None = None
    ) -> None:
        self._require_initialized()
        validate_event_schema(event)
        effective_priority = priority if priority is not None else event.priority
        event = Event(
            topic=event.topic,
            payload=event.payload,
            event_id=event.event_id,
            source_module=event.source_module,
            priority=effective_priority,
            created_at=event.created_at,
            correlation_id=event.correlation_id,
            schema_version=event.schema_version,
        )
        self._known_topics.add(event.topic)
        self._last_event_time = datetime.now(timezone.utc).isoformat()
        maxlen = _stream_maxlen(event.topic)
        if maxlen is None:
            await self._redis.xadd(_stream_key(event.topic), event.to_wire())
        else:
            await self._redis.xadd(
                _stream_key(event.topic),
                event.to_wire(),
                maxlen=maxlen,
                approximate=True,
            )
        logger.info(
            "published event",
            extra={"aitos_extra": {"topic": event.topic, "event_id": event.event_id}},
        )
        if (
            event.topic.endswith(".reply")
            and event.correlation_id
            and event.correlation_id in self._pending_replies
        ):
            fut = self._pending_replies.pop(event.correlation_id)
            if not fut.done():
                fut.set_result(event)

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        group: str = "default",
        start_id: str = "0",
    ) -> Subscription:
        """Subscribe to a Redis Stream.

        ``start_id='0'`` preserves the historical/replay semantics used by
        durable consumers. ``start_id='$'`` is an explicit live-only mode:
        the group cursor is moved to the current stream tail even when the
        group already exists, and abandoned pending entries are not reclaimed.
        """
        self._require_initialized()
        consumer_name = f"{group}-{id(handler)}"
        live_only = start_id == "$"
        if "*" in topic:
            resolved_topics = [
                t for t in self._known_topics if fnmatch.fnmatch(t, topic)
            ]
        else:
            resolved_topics = [topic]
            self._known_topics.add(topic)
        for t in resolved_topics or [topic]:
            await self._ensure_group(
                _stream_key(t), group, start_id=start_id, reset_existing=live_only
            )
        task = asyncio.create_task(
            self._consume_loop(
                topic_pattern=topic,
                group=group,
                consumer=consumer_name,
                handler=handler,
                live_only=live_only,
                start_id=start_id,
            )
        )
        sub = Subscription(
            topic_pattern=topic, group=group, consumer=consumer_name, _task=task
        )
        self._subscriptions.append(sub)
        return sub

    async def request_reply(
        self, event: Event, timeout_ms: float = 5000
    ) -> EventResponse:
        self._require_initialized()
        correlation_id = event.correlation_id or event.event_id
        request_event = Event(
            topic=event.topic,
            payload=event.payload,
            event_id=event.event_id,
            source_module=event.source_module,
            priority=event.priority,
            correlation_id=correlation_id,
        )
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_replies[correlation_id] = fut
        await self.publish(request_event, priority=request_event.priority)
        try:
            reply_event: Event = await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            self._pending_replies.pop(correlation_id, None)
            raise TimeoutError(
                f"request_reply timed out after {timeout_ms}ms for topic {event.topic}"
            ) from exc
        return EventResponse(
            request_event_id=event.event_id,
            responder_module=reply_event.source_module,
            payload=reply_event.payload,
            success=True,
        )

    async def replay(self, topic: str, since: datetime, handler: EventHandler) -> None:
        self._require_initialized()
        since_ms = int(since.timestamp() * 1000)
        entries = await self._redis.xrange(_stream_key(topic), min=f"{since_ms}-0")
        for entry_id, fields in entries:
            event = Event.from_wire(fields)
            await handler(event)

    async def _ensure_group(
        self,
        stream_key: str,
        group: str,
        *,
        start_id: str = "0",
        reset_existing: bool = False,
    ) -> None:
        key = (stream_key, group)
        if key in self._ensured_groups:
            if reset_existing:
                # Live-only subscribers must move their cursor on every
                # explicit subscription, even if this process already ensured
                # the group earlier.
                await self._redis.xgroup_setid(stream_key, group, id="$")
            return
        try:
            await self._redis.xgroup_create(
                stream_key, group, id=start_id, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            if reset_existing:
                await self._redis.xgroup_setid(stream_key, group, id="$")
        finally:
            self._ensured_groups.add(key)

    async def _reclaim_pending(
        self, stream_key: str, group: str, consumer: str
    ) -> list[tuple[Any, dict[str, Any]]]:
        try:
            result = await self._redis.xautoclaim(
                stream_key,
                group,
                consumer,
                min_idle_time=PENDING_RECLAIM_IDLE_MS,
                start_id="0-0",
                count=PENDING_RECLAIM_BATCH_SIZE,
            )
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                return list(result[1])
        except (AttributeError, TypeError):
            pass
        except Exception as exc:
            logger.warning(
                "pending reclaim failed",
                extra={
                    "aitos_extra": {
                        "stream": stream_key,
                        "group": group,
                        "error": str(exc),
                    }
                },
            )
        return []

    async def _consume_loop(
        self,
        topic_pattern: str,
        group: str,
        consumer: str,
        handler: EventHandler,
        *,
        live_only: bool = False,
        start_id: str = "0",
    ) -> None:
        streams_seen: set[str] = set()
        try:
            while True:
                if "*" in topic_pattern:
                    topics = [
                        t
                        for t in self._known_topics
                        if fnmatch.fnmatch(t, topic_pattern)
                    ]
                else:
                    topics = [topic_pattern]
                stream_names = [_stream_key(t) for t in topics]
                for stream_key in stream_names:
                    if stream_key not in streams_seen:
                        await self._ensure_group(
                            stream_key,
                            group,
                            start_id=start_id,
                            reset_existing=live_only,
                        )
                        streams_seen.add(stream_key)
                if not stream_names:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue
                pending_entries: list[tuple[str, Any, dict[str, Any]]] = []
                if not live_only:
                    for stream_key in stream_names:
                        for entry_id, fields in await self._reclaim_pending(
                            stream_key, group, consumer
                        ):
                            pending_entries.append((stream_key, entry_id, fields))
                if pending_entries:
                    batches = [(s, i, f) for s, i, f in pending_entries]
                else:
                    result = await self._redis.xreadgroup(
                        group,
                        consumer,
                        {s: ">" for s in stream_names},
                        count=CONSUMER_BATCH_SIZE,
                        block=CONSUMER_BLOCK_MS,
                    )
                    batches = [
                        (stream_key, entry_id, fields)
                        for stream_key, entries in result
                        for entry_id, fields in entries
                    ]
                for stream_key, entry_id, fields in batches:
                    try:
                        event = Event.from_wire(fields)
                        response = await handler(event)
                        await self._redis.xack(stream_key, group, entry_id)
                        if response is not None:
                            await self._maybe_publish_response(event, response)
                    except Exception as exc:
                        logger.exception(
                            "event handler failed",
                            extra={
                                "aitos_extra": {
                                    "stream": stream_key,
                                    "group": group,
                                    "entry_id": entry_id,
                                    "error": str(exc),
                                }
                            },
                        )
                        await self._handle_failed_event(
                            stream_key, group, entry_id, fields, exc
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("event consumer stopped: %s", exc)

    async def _maybe_publish_response(
        self, request: Event, response: EventResponse
    ) -> None:
        if not response.success:
            return
        reply_topic = request.topic + ".reply"
        reply_event = Event(
            topic=reply_topic,
            payload=response.payload,
            source_module=response.responder_module,
            correlation_id=request.correlation_id or request.event_id,
        )
        await self.publish(reply_event)

    async def _handle_failed_event(
        self,
        stream_key: str,
        group: str,
        entry_id: Any,
        fields: dict[str, Any],
        exc: Exception,
    ) -> None:
        attempts = int(fields.get("_delivery_attempts", 0)) + 1
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            dlq_fields = dict(fields)
            dlq_fields.update(
                {
                    "original_stream": stream_key,
                    "consumer_group": group,
                    "error": str(exc),
                    "_delivery_attempts": attempts,
                }
            )
            await self._redis.xadd(
                DLQ_STREAM, dlq_fields, maxlen=25_000, approximate=True
            )
            await self._redis.xack(stream_key, group, entry_id)
        else:
            # Keep the entry pending; the next reclaim will retry it.
            await self._redis.xadd(
                stream_key,
                {**fields, "_delivery_attempts": attempts},
                maxlen=_stream_maxlen(stream_key.removeprefix("stream:")) or 25_000,
                approximate=True,
            )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(f"{self.module_id} is not initialized")


async def _await_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
