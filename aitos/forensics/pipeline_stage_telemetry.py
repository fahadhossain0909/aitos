"""Forensic telemetry for WebSocket receive and Redis write boundaries."""
from __future__ import annotations

import asyncio
import json
import time
from functools import wraps
from typing import Any

_INSTALLED = False
_LOGGER_NAME = "aitos.forensics.e2e"


def _logger():
    from aitos.logging_setup import get_logger

    return get_logger(_LOGGER_NAME)


def _trace_id(envelope: Any, stream: str = "") -> str | None:
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data", envelope)
    if not isinstance(data, dict):
        return None
    symbol = str(data.get("s") or data.get("symbol") or stream.split("@", 1)[0]).upper()
    event_id = data.get("a") or data.get("l") or data.get("u") or data.get("E")
    return f"md:{symbol}:{event_id}" if symbol and event_id is not None else None


class _WSReceiveProxy:
    def __init__(self, websocket: Any, adapter: Any) -> None:
        self._websocket = websocket
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._websocket, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        started = time.perf_counter()
        received_at_ms = time.time() * 1000
        message = await self._websocket.__anext__()
        wait_ms = (time.perf_counter() - started) * 1000
        self._adapter._e2e_ws_messages = getattr(self._adapter, "_e2e_ws_messages", 0) + 1
        stream = ""
        event_ms = None
        trace_id = None
        size = len(message) if isinstance(message, (str, bytes)) else 0
        if isinstance(message, str):
            try:
                envelope = json.loads(message)
                stream = str(envelope.get("stream", ""))
                data = envelope.get("data", envelope)
                if isinstance(data, dict):
                    event_ms = data.get("T", data.get("E"))
                trace_id = _trace_id(envelope, stream)
            except Exception:
                pass
        source_age_ms = None
        if event_ms is not None:
            try:
                source_age_ms = max(0.0, received_at_ms - float(event_ms))
            except (TypeError, ValueError):
                pass
        if source_age_ms is not None and source_age_ms >= 1000:
            _logger().warning(
                "market-data websocket receive lag",
                extra={"aitos_extra": {
                    "stage": "ws_receive",
                    "trace_id": trace_id,
                    "stream": stream,
                    "exchange_event_ms": event_ms,
                    "received_at_ms": round(received_at_ms, 3),
                    "source_age_ms": round(source_age_ms, 3),
                    "receive_wait_ms": round(wait_ms, 3),
                    "bytes": size,
                }},
            )
        return message


class _WSContextProxy:
    def __init__(self, context: Any, adapter: Any) -> None:
        self._context = context
        self._adapter = adapter
        self._websocket: Any = None

    def __getattr__(self, name: str) -> Any:
        target = self._websocket if self._websocket is not None else self._context
        return getattr(target, name)

    async def __aenter__(self):
        self._websocket = await self._context.__aenter__()
        return _WSReceiveProxy(self._websocket, self._adapter)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._context.__aexit__(exc_type, exc, tb)


def _cgroup_cpu() -> dict[str, int] | None:
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        try:
            result: dict[str, int] = {}
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        result[parts[0]] = int(parts[1])
            if result:
                return result
        except (OSError, ValueError):
            continue
    return None


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_binance()
    _install_eventbus()


def _install_binance() -> None:
    try:
        from aitos.exchange import binance
    except Exception:
        return
    cls = binance.BinanceFuturesAdapter
    if getattr(cls, "_e2e_ws_receive_installed", False):
        return
    cls._e2e_ws_receive_installed = True
    original_init = cls.__init__

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        connector = self._ws_connector

        def traced_connector(url: str, *cargs: Any, **ckwargs: Any):
            return _WSContextProxy(connector(url, *cargs, **ckwargs), self)

        self._ws_connector = traced_connector
        self._e2e_ws_messages = 0

    cls.__init__ = init


def _install_eventbus() -> None:
    try:
        from aitos.eventbus.redis_bus import EventBus
    except Exception:
        return
    if getattr(EventBus, "_e2e_redis_write_installed", False):
        return
    EventBus._e2e_redis_write_installed = True
    original_init = EventBus.__init__
    original_health = EventBus.health_check

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._e2e_redis_stats = {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "timeouts": 0}
        redis = getattr(self, "_redis", None)
        original_xadd = getattr(redis, "xadd", None)
        if redis is None or original_xadd is None:
            return

        async def traced_xadd(*xargs: Any, **xkwargs: Any):
            started = time.perf_counter()
            try:
                return await original_xadd(*xargs, **xkwargs)
            except asyncio.TimeoutError:
                self._e2e_redis_stats["timeouts"] += 1
                raise
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                stats = self._e2e_redis_stats
                stats["count"] += 1
                stats["total_ms"] += elapsed
                stats["max_ms"] = max(stats["max_ms"], elapsed)
                if elapsed >= 100:
                    _logger().warning(
                        "redis xadd latency",
                        extra={"aitos_extra": {
                            "stage": "redis_xadd",
                            "stream": str(xargs[0]) if xargs else str(xkwargs.get("name", "")),
                            "latency_ms": round(elapsed, 3),
                            "cgroup_cpu": _cgroup_cpu(),
                        }},
                    )

        redis.xadd = traced_xadd

    @wraps(original_health)
    async def health(self: Any, *args: Any, **kwargs: Any):
        status = await original_health(self, *args, **kwargs)
        try:
            from dataclasses import replace

            details = dict(status.details)
            stats = dict(getattr(self, "_e2e_redis_stats", {}))
            count = stats.get("count", 0)
            stats["avg_ms"] = round(stats.get("total_ms", 0.0) / count, 3) if count else 0.0
            stats["total_ms"] = round(stats.get("total_ms", 0.0), 3)
            stats["max_ms"] = round(stats.get("max_ms", 0.0), 3)
            stats["cgroup_cpu"] = _cgroup_cpu()
            details.setdefault("market_data_e2e", {})["redis_xadd"] = stats
            return replace(status, details=details)
        except Exception:
            return status

    EventBus.__init__ = init
    EventBus.health_check = health


__all__ = ["install"]
