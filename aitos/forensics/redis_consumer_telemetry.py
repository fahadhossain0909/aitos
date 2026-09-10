"""Observational Redis consumer-group diagnostics for root-cause analysis."""

from __future__ import annotations

import time
from typing import Any

from aitos.logging_setup import get_logger

logger = get_logger("aitos.forensics.redis_consumers")

_MAX_GROUPS = 64
_MAX_CONSUMERS_PER_GROUP = 64


def _decode(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


def install(event_bus_cls: type[Any]) -> None:
    """Expose Redis XINFO consumer inventory through EventBus health.

    This is deliberately read-only: it never deletes consumers/groups and never
    changes acknowledgement, reclaim, concurrency, or stream retention policy.
    """
    if getattr(event_bus_cls, "_consumer_forensics_installed", False):
        return
    event_bus_cls._consumer_forensics_installed = True
    original_health = event_bus_cls.health_check

    async def health(self: Any, *args: Any, **kwargs: Any):
        from dataclasses import replace

        status = await original_health(self, *args, **kwargs)
        details = dict(status.details)
        inventory: list[dict[str, Any]] = []
        redis = getattr(self, "_redis", None)
        topics = sorted(getattr(self, "_known_topics", set()))[:_MAX_GROUPS]
        if redis is not None:
            for topic in topics:
                stream = f"stream:{topic}"
                try:
                    groups = await redis.xinfo_groups(stream)
                except Exception as exc:
                    inventory.append({"stream": stream, "error": str(exc)[:200]})
                    continue
                for raw_group in groups[:_MAX_GROUPS]:
                    group = {_decode(k): _decode(v) for k, v in raw_group.items()}
                    group_name = str(group.get("name", ""))
                    try:
                        consumers = await redis.xinfo_consumers(stream, group_name)
                    except Exception as exc:
                        consumers = []
                        group["consumer_error"] = str(exc)[:200]
                    consumer_rows = []
                    now_ms = int(time.time() * 1000)
                    for raw_consumer in consumers[:_MAX_CONSUMERS_PER_GROUP]:
                        consumer = {
                            _decode(k): _decode(v) for k, v in raw_consumer.items()
                        }
                        idle_ms = consumer.get("idle")
                        consumer_rows.append(
                            {
                                "name": consumer.get("name"),
                                "pending": consumer.get("pending", 0),
                                "idle_ms": idle_ms,
                                "idle_at_ms": (
                                    now_ms - int(idle_ms)
                                    if isinstance(idle_ms, (int, float))
                                    else None
                                ),
                            }
                        )
                    inventory.append(
                        {
                            "stream": stream,
                            "group": group_name,
                            "pending": group.get("pending", 0),
                            "lag": group.get("lag"),
                            "entries_read": group.get("entries-read"),
                            "last_delivered_id": _decode(
                                group.get("last-delivered-id")
                            ),
                            "consumers": consumer_rows,
                        }
                    )
        details["consumer_forensics"] = {
            "streams_examined": len(topics),
            "groups": inventory,
        }
        return replace(status, details=details)

    event_bus_cls.health_check = health
