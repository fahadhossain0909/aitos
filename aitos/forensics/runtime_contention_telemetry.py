"""Runtime contention telemetry for Redis, event-loop scheduling, and persistence.

Observational only. This module intentionally does not change concurrency, CPU
limits, retries, ordering, or freshness policy. It adds measurements needed to
decide whether the next bottleneck is application code, Redis, or CPU quota.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import replace
from functools import wraps
from typing import Any

_INSTALLED = False


def _logger():
    from aitos.logging_setup import get_logger

    return get_logger("aitos.forensics.contention")


def _cgroup_cpu() -> dict[str, int] | None:
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        try:
            values: dict[str, int] = {}
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2:
                        try:
                            values[parts[0]] = int(parts[1])
                        except ValueError:
                            pass
            return values or None
        except OSError:
            continue
    return None


def _cgroup_memory() -> dict[str, int] | None:
    for path in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                return {"current_bytes": int(handle.read().strip())}
        except (OSError, ValueError):
            continue
    return None


def _record(bucket: dict[str, Any], elapsed_ms: float) -> None:
    bucket["count"] += 1
    bucket["total_ms"] += elapsed_ms
    bucket["max_ms"] = max(bucket["max_ms"], elapsed_ms)
    bucket["min_ms"] = elapsed_ms if bucket["min_ms"] is None else min(bucket["min_ms"], elapsed_ms)
    bucket["over_100ms"] += elapsed_ms >= 100
    bucket["over_1000ms"] += elapsed_ms >= 1000


def _bucket() -> dict[str, Any]:
    return {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "min_ms": None, "over_100ms": 0, "over_1000ms": 0}


def _format(bucket: dict[str, Any]) -> dict[str, Any]:
    count = int(bucket["count"])
    out = dict(bucket)
    out["avg_ms"] = round(bucket["total_ms"] / count, 3) if count else 0.0
    out["total_ms"] = round(bucket["total_ms"], 3)
    out["max_ms"] = round(bucket["max_ms"], 3)
    if bucket["min_ms"] is not None:
        out["min_ms"] = round(bucket["min_ms"], 3)
    return out


def _start_watchdog(event_bus: Any) -> None:
    if getattr(event_bus, "_contention_watchdog_task", None) is not None:
        return
    event_bus._contention_watchdog_task = asyncio.create_task(
        _watchdog(event_bus), name="aitos-event-loop-contention-watchdog"
    )


async def _watchdog(event_bus: Any) -> None:
    loop = asyncio.get_running_loop()
    interval = 0.5
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        lag_ms = max(0.0, (now - expected) * 1000)
        expected = now + interval
        stats = getattr(event_bus, "_contention_loop", _bucket())
        event_bus._contention_loop = stats
        _record(stats, lag_ms)
        event_bus._contention_last = {
            "lag_ms": round(lag_ms, 3),
            "at_ms": round(time.time() * 1000, 3),
            "cgroup_cpu": _cgroup_cpu(),
        }
        if lag_ms >= 100:
            _logger().warning("event-loop scheduling lag", extra={"aitos_extra": event_bus._contention_last})


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        from aitos.eventbus.redis_bus import EventBus
    except Exception:
        return
    _install_eventbus(EventBus)
    _install_repository()


def _install_eventbus(cls: type[Any]) -> None:
    if getattr(cls, "_contention_telemetry_installed", False):
        return
    cls._contention_telemetry_installed = True
    original_init = cls.__init__
    original_initialize = cls.initialize
    original_health = cls.health_check

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._contention_loop = _bucket()
        self._contention_last = None
        self._contention_server = {}
        self._contention_server_recent = deque(maxlen=20)
        self._contention_watchdog_task = None

    @wraps(original_initialize)
    async def initialize(self: Any, *args: Any, **kwargs: Any):
        result = await original_initialize(self, *args, **kwargs)
        _start_watchdog(self)
        return result

    @wraps(original_health)
    async def health(self: Any, *args: Any, **kwargs: Any):
        status = await original_health(self, *args, **kwargs)
        details = dict(status.details)
        redis = getattr(self, "_redis", None)
        server: dict[str, Any] = {}
        if redis is not None:
            try:
                info = await redis.info(section="server")
                server.update({k: info.get(k) for k in ("redis_version", "hz", "uptime_in_seconds") if k in info})
            except Exception as exc:
                server["error"] = f"{type(exc).__name__}: {exc}"
            try:
                stats = await redis.info(section="stats")
                for key in ("instantaneous_ops_per_sec", "instantaneous_input_kbps", "instantaneous_output_kbps", "rejected_connections", "blocked_clients"):
                    if key in stats:
                        server[key] = stats[key]
            except Exception as exc:
                server["stats_error"] = f"{type(exc).__name__}: {exc}"
            try:
                clients = await redis.info(section="clients")
                for key in ("connected_clients", "blocked_clients"):
                    if key in clients:
                        server[key] = clients[key]
            except Exception:
                pass
        self._contention_server = server
        self._contention_server_recent.append({"at_ms": round(time.time() * 1000, 3), **server})
        details["runtime_contention"] = {
            "event_loop": _format(self._contention_loop),
            "event_loop_last": self._contention_last,
            "redis_server": server,
            "redis_server_recent": list(self._contention_server_recent),
            "cgroup_cpu": _cgroup_cpu(),
            "cgroup_memory": _cgroup_memory(),
        }
        return replace(status, details=details)

    cls.__init__ = init
    cls.initialize = initialize
    cls.health_check = health


def _install_repository() -> None:
    try:
        from aitos.data.repository import MarketDataRepository
    except Exception:
        return
    cls = MarketDataRepository
    if getattr(cls, "_contention_batch_telemetry_installed", False):
        return
    cls._contention_batch_telemetry_installed = True
    original_init = cls.__init__
    original_health = cls.health_check

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._contention_batch_stats = {}

    def wrap(name: str, original: Any):
        @wraps(original)
        async def wrapped(self: Any, *args: Any, **kwargs: Any):
            started = time.perf_counter()
            count = len(args[0]) if args and isinstance(args[0], (list, tuple)) else None
            try:
                return await original(self, *args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                bucket = self._contention_batch_stats.setdefault(name, _bucket())
                _record(bucket, elapsed)
                if count is not None:
                    bucket["rows"] = bucket.get("rows", 0) + count
                if elapsed >= 100:
                    _logger().warning("clickhouse batch write latency", extra={"aitos_extra": {"operation": name, "latency_ms": round(elapsed, 3), "rows": count, "cgroup_cpu": _cgroup_cpu()}})

        return wrapped

    @wraps(original_health)
    async def health(self: Any, *args: Any, **kwargs: Any):
        status = await original_health(self, *args, **kwargs)
        details = dict(status.details)
        details.setdefault("runtime_contention", {})["clickhouse_batches"] = {
            name: _format(stats) for name, stats in getattr(self, "_contention_batch_stats", {}).items()
        }
        return replace(status, details=details)

    cls.__init__ = init
    cls.health_check = health
    for name in ("save_trade_ticks", "save_order_book_snapshots"):
        original = getattr(cls, name, None)
        if original is not None:
            setattr(cls, name, wrap(name, original))


__all__ = ["install"]
