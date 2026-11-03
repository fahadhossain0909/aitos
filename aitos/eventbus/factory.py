"""Explicit EventBus backend selection with safe trading-mode guards."""
from __future__ import annotations

from typing import Any

from .contracts import EventBusProtocol
from .inprocess_bus import InProcessBus
from .redis_bus import EventBus as RedisEventBus


_VOLATILE_FORBIDDEN_MODES = {"paper", "live", "production", "prod"}


def create_event_bus(
    config: dict[str, Any] | None = None, *, redis_client: Any | None = None
) -> EventBusProtocol:
    """Create the configured transport; Redis remains the safe default.

    Supported backends: ``redis`` and ``inprocess``. The latter is rejected
    for paper/live/production modes because it has no persistence or recovery.
    NATS is deliberately rejected until its JetStream adapter and contract
    tests are implemented; it is never silently substituted.
    """
    options = dict(config or {})
    backend = str(options.get("backend", "redis")).strip().lower()
    runtime_mode = str(options.get("runtime_mode", "development")).strip().lower()

    if backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required when EventBus backend is redis")
        return RedisEventBus(
            redis_client=redis_client,
            module_id=str(options.get("module_id", "event-bus"))
        )

    if backend == "inprocess":
        if runtime_mode in _VOLATILE_FORBIDDEN_MODES:
            raise ValueError(
                "InProcessBus is volatile and forbidden in paper/live/production mode"
            )
        return InProcessBus(
            history_limit=int(options.get("history_limit", 10_000)),
            queue_limit=int(options.get("queue_limit", 10_000)),
            module_id=str(options.get("module_id", "event-bus-inprocess")),
        )

    if backend in {"nats", "jetstream"}:
        raise NotImplementedError(
            "NATS JetStream backend is not available until its adapter is implemented"
        )
    raise ValueError(f"Unsupported EventBus backend: {backend!r}")
