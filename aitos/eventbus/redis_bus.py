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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (Any, AsyncIterator, Awaitable, Callable, Dict, List,
                    Optional)

from aitos.core.contracts import (AITOSModule, Event, EventPriority,
                                  EventResponse, HealthStatus, ModuleStatus)
from aitos.core.exceptions import (EventSchemaValidationError,
                                   ModuleNotInitializedError)
from aitos.logging_setup import get_logger

logger = get_logger("aitos.eventbus")

EventHandler = Callable[[Event], Awaitable[Optional[EventResponse]]]

DLQ_STREAM = "stream:dlq"
MAX_DELIVERY_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 0.15

# High-volume market streams are transient transport buffers. Historical market
# data belongs in ClickHouse; trade/decision/journal streams remain unbounded.
# Defaults are deliberately conservative for the 8–12 GiB VPS profile and can
# be overridden without a code change through environment variables.
STREAM_MAXLEN_DEFAULTS = {
    "market.orderbook.": 25_000,
    "market.liquidity.": 100_000,
    "market.live_state.": 25_000,
}


def _stream_key(topic: str) -> str:
    return f"stream:{topic}"


def _stream_maxlen(topic: str) -> Optional[int]:
    """Return the configured bounded length for high-volume market topics."""
    for prefix, default in STREAM_MAXLEN_DEFAULTS.items():
        if topic.startswith(prefix):
            env_name = "REDIS_STREAM_MAXLEN_" + prefix[:-1].replace(".", "_").upper()
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
    """Minimal structural validation. Extend per-topic schemas as needed."""
    if not event.topic or not isinstance(event.topic, str):
        raise EventSchemaValidationError("Event.topic must be a non-empty string")
    if not isinstance(event.payload, dict):
        raise EventSchemaValidationError("Event.payload must be a dict")


@dataclass
class Subscription:
    """Handle returned by ``subscribe``; call ``cancel()`` to stop consuming."""

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
        self._started_at: Optional[float] = None
        self._last_event_time: Optional[str] = None
        self._known_topics: set[str] = set()
        self._subscriptions: List[Subscription] = []
        self._pending_replies: Dict[str, asyncio.Future] = {}

    @property
    def module_id(self) -> str:
        return self._module_id

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
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
        except Exception as exc:  # noqa: BLE001
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
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    async def publish(
        self, event: Event, priority: Optional[EventPriority] = None
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
        self, topic: str, handler: EventHandler, group: str = "default"
    ) -> Subscription:
        """Subscribe to a topic (supports ``*`` glob patterns, e.g. ``intel.*``)."""
        self._require_initialized()
        consumer_name = f"{group}-{id(handler)}"

        if "*" in topic:
            resolved_topics = [
                t for t in self._known_topics if fnmatch.fnmatch(t, topic)
            ]
        else:
            resolved_topics = [topic]
            self._known_topics.add(topic)

        for t in resolved_topics or [topic]:
            await self._ensure_group(_stream_key(t), group)

        task = asyncio.create_task(
            self._consume_loop(
                topic_pattern=topic,
                group=group,
                consumer=consumer_name,
                handler=handler,
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
        """Publish ``event`` and await a correlated reply within ``timeout_ms``."""
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

    async def _ensure_group(self, stream_key: str, group: str) -> None:
        try:
            await self._redis.xgroup_create(stream_key, group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_loop(
        self, topic_pattern: str, group: str, consumer: str, handler: EventHandler
    ) -> None:
        streams_seen: set[str] = set()
        try:
            while True:
                if "*" in topic_pattern:
                    matching = {
                        t
                        for t in self._known_topics
                        if fnmatch.fnmatch(t, topic_pattern)
                    }
                    new_streams = matching - streams_seen
                    for t in new_streams:
                        await self._ensure_group(_stream_key(t), group)
                    streams_seen |= matching
                else:
                    streams_seen = {topic_pattern}

                if not streams_seen:
                    await asyncio.sleep(0.2)
                    continue

                stream_map = {_stream_key(t): ">" for t in streams_seen}
                try:
                    resp = await self._redis.xreadgroup(
                        groupname=group,
                        consumername=consumer,
                        streams=stream_map,
                        count=10,
                        block=None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("xreadgroup error: %s", exc)
                    await asyncio.sleep(1.0)
                    continue

                if not resp:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                for stream_key, messages in resp:
                    stream_key = (
                        stream_key.decode()
                        if isinstance(stream_key, bytes)
                        else stream_key
                    )
                    for entry_id, fields in messages:
                        await self._process_message(
                            stream_key, entry_id, fields, group, handler
                        )
        except asyncio.CancelledError:
            return

    async def _process_message(
        self,
        stream_key: str,
        entry_id: Any,
        fields: Dict[str, Any],
        group: str,
        handler: EventHandler,
    ) -> None:
        event = Event.from_wire(fields)
        try:
            response = await handler(event)
            await self._redis.xack(stream_key, group, entry_id)
            if response is not None and event.correlation_id:
                reply_event = Event(
                    topic=f"{event.topic}.reply",
                    payload=response.payload,
                    source_module=response.responder_module,
                    correlation_id=event.correlation_id,
                )
                await self.publish(reply_event)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "handler failed for event",
                extra={
                    "aitos_extra": {
                        "topic": event.topic,
                        "event_id": event.event_id,
                        "error": str(exc),
                    }
                },
            )
            await self._maybe_dead_letter(
                stream_key, entry_id, fields, group, event, exc
            )

    async def _maybe_dead_letter(
        self,
        stream_key: str,
        entry_id: Any,
        fields: Dict[str, Any],
        group: str,
        event: Event,
        exc: Exception,
    ) -> None:
        pending = await self._redis.xpending_range(
            stream_key, group, min="-", max="+", count=1, consumername=None
        )
        delivery_count = 1
        for p in pending:
            pid = p.get("message_id") if isinstance(p, dict) else None
            if pid == entry_id:
                delivery_count = p.get("times_delivered", 1)
                break

        if delivery_count >= MAX_DELIVERY_ATTEMPTS:
            dlq_payload = dict(fields)
            dlq_payload["dlq_reason"] = str(exc)
            dlq_payload["original_stream"] = stream_key
            await self._redis.xadd(DLQ_STREAM, dlq_payload)
            await self._redis.xack(stream_key, group, entry_id)
            logger.error(
                "event moved to DLQ",
                extra={
                    "aitos_extra": {"topic": event.topic, "event_id": event.event_id}
                },
            )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "EventBus.initialize() must be called first"
            )


async def _await_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
