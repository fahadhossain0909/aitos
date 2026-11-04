"""Volatile in-process EventBus for tests and single-process development.

This transport intentionally does not provide persistence, cross-process
delivery, durable ACKs, or crash recovery. Use Redis Streams for paper/live
runtimes until a durable backend is explicitly configured.
"""
 
from __future__ import annotations

import asyncio
import fnmatch
from collections import defaultdict, deque
from collections.abc import AsyncIterator
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
from aitos.core.exceptions import (
    EventSchemaValidationError,
    ModuleNotInitializedError,
)

from .contracts import EventHandler


@dataclass
class InProcessSubscription:
    topic_pattern: str
    group: str
    _queue: asyncio.Queue[Event]
    _task: asyncio.Task | None = None

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class InProcessBus(AITOSModule):
    """Async, bounded, process-local event transport.

    Each subscription receives its own copy of matching events. This is
    deliberately broadcast semantics (not Redis consumer-group load sharing)
    and is suitable for deterministic unit tests, not production trading.
    """

    def __init__(
        self,
        *,
        history_limit: int = 10_000,
        queue_limit: int = 10_000,
        module_id: str = "event-bus-inprocess",
    ) -> None:
        if history_limit < 1 or queue_limit < 1:
            raise ValueError("history_limit and queue_limit must be >= 1")
        self._history_limit = history_limit
        self._queue_limit = queue_limit
        self._module_id = module_id
        self._initialized = False
        self._history: dict[str, deque[Event]] = defaultdict(
            lambda: deque(maxlen=self._history_limit)
        )
        self._subscriptions: list[InProcessSubscription] = []
        self._pending_replies: dict[str, asyncio.Future[Event]] = {}
        self._published = 0
        self._consumed = 0
        self._handler_failures = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return self._module_id

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "transport": "inprocess",
                "durable": False,
                "published_events": self._published,
                "consumed_events": self._consumed,
                "handler_failures": self._handler_failures,
                "subscriptions": len(self._subscriptions),
                "history_limit_per_topic": self._history_limit,
                "queue_limit_per_subscription": self._queue_limit,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        tasks = [sub._task for sub in self._subscriptions if sub._task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscriptions.clear()
        for future in self._pending_replies.values():
            if not future.done():
                future.cancel()
        self._pending_replies.clear()
        self._initialized = False

    async def emit_events(self) -> AsyncIterator[Event]:
        if False:
            yield Event(topic="__never__", payload={})

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def publish(
        self, event: Event, priority: EventPriority | None = None
    ) -> None:
        self._require_initialized()
        if not isinstance(event.topic, str) or not event.topic:
            raise EventSchemaValidationError("Event.topic must be a non-empty string")
        if not isinstance(event.payload, dict):
            raise EventSchemaValidationError("Event.payload must be a dict")
        if priority is not None and priority != event.priority:
            event = Event(
                topic=event.topic,
                payload=event.payload,
                event_id=event.event_id,
                source_module=event.source_module,
                priority=priority,
                created_at=event.created_at,
                correlation_id=event.correlation_id,
                schema_version=event.schema_version,
            )
        self._history[event.topic].append(event)
        self._published += 1
        self._last_event_time = datetime.now(timezone.utc).isoformat()
        if event.topic.endswith(".reply") and event.correlation_id:
            future = self._pending_replies.pop(event.correlation_id, None)
            if future is not None and not future.done():
                future.set_result(event)
        for sub in tuple(self._subscriptions):
            if sub._task is None or sub._task.done():
                self._remove_subscription(sub)
                continue
            if fnmatch.fnmatch(event.topic, sub.topic_pattern):
                await sub._queue.put(event)

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        group: str = "default",
        start_id: str = "0",
    ) -> InProcessSubscription:
        self._require_initialized()
        if start_id not in {"0", "$"}:
            raise ValueError("InProcessBus supports start_id='0' or '$' only")
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_limit)
        sub = InProcessSubscription(topic_pattern=topic, group=group, _queue=queue)
        initial_events: list[Event] = []
        if start_id == "0":
            for stored_topic, events in self._history.items():
                if fnmatch.fnmatch(stored_topic, topic):
                    initial_events.extend(events)

        async def _dispatch(event: Event) -> None:
            try:
                response = await handler(event)
                self._consumed += 1
                if response is not None and response.success:
                    await self.publish(
                        Event(
                            topic=event.topic + ".reply",
                            payload=response.payload,
                            source_module=response.responder_module,
                            correlation_id=event.correlation_id or event.event_id,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._handler_failures += 1

        async def _consume() -> None:
            for event in initial_events:
                await _dispatch(event)
            while True:
                event = await queue.get()
                try:
                    await _dispatch(event)
                finally:
                    queue.task_done()

        task = asyncio.create_task(_consume(), name=f"inprocess-{group}-{topic}")
        sub._task = task
        self._subscriptions.append(sub)
        task.add_done_callback(lambda _: self._remove_subscription(sub))
        return sub

    async def request_reply(
        self, event: Event, timeout_ms: float = 5000
    ) -> EventResponse:
        self._require_initialized()
        correlation_id = event.correlation_id or event.event_id
        request = Event(
            topic=event.topic,
            payload=event.payload,
            event_id=event.event_id,
            source_module=event.source_module,
            priority=event.priority,
            created_at=event.created_at,
            correlation_id=correlation_id,
            schema_version=event.schema_version,
        )
        future = asyncio.get_running_loop().create_future()
        self._pending_replies[correlation_id] = future
        try:
            await self.publish(request)
            reply = await asyncio.wait_for(future, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"request_reply timed out for topic {event.topic}"
            ) from exc
        finally:
            self._pending_replies.pop(correlation_id, None)
        return EventResponse(
            request_event_id=event.event_id,
            responder_module=reply.source_module,
            payload=reply.payload,
            success=True,
        )

    async def replay(self, topic: str, since: datetime, handler: EventHandler) -> None:
        self._require_initialized()
        for event in tuple(self._history.get(topic, ())):
            created = datetime.fromisoformat(event.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= since:
                await handler(event)

    def _remove_subscription(self, subscription: InProcessSubscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(f"{self.module_id} is not initialized")
