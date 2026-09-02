"""Forensic telemetry for market-data receive, processing, and Redis boundaries.

This module is intentionally observational. It does not drop, reorder, retry, or
coalesce market-data events. It records enough timing information to distinguish
exchange/network delay from client buffering, event-loop starvation, application
processing, and Redis backpressure.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
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
    # AITOS TradeTick uses Binance's last raw trade id (l), not aggregate id a.
    event_id = data.get("l") or data.get("t") or data.get("u") or data.get("E")
    return f"md:{symbol}:{event_id}" if symbol and event_id is not None else None


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record(stats: dict[str, dict[str, float]], key: str, value: float) -> None:
    row = stats.setdefault(
        key,
        {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0, "min_ms": value},
    )
    row["count"] += 1
    row["total_ms"] += value
    row["max_ms"] = max(row["max_ms"], value)
    row["min_ms"] = min(row["min_ms"], value)


def _format(stats: dict[str, dict[str, float]]) -> dict[str, dict[str, float | int]]:
    return {
        key: {
            "count": int(row["count"]),
            "total_ms": round(row["total_ms"], 3),
            "avg_ms": round(row["total_ms"] / row["count"], 3)
            if row["count"]
            else 0.0,
            "max_ms": round(row["max_ms"], 3),
            "min_ms": round(row["min_ms"], 3),
        }
        for key, row in sorted(stats.items())
    }


def _cgroup_stats() -> dict[str, int] | None:
    """Read Linux cgroup CPU/memory pressure counters without failing startup."""
    paths = (
        "/sys/fs/cgroup/cpu.stat",
        "/sys/fs/cgroup/cpu/cpu.stat",
    )
    for path in paths:
        try:
            result: dict[str, int] = {}
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2:
                        try:
                            result[parts[0]] = int(parts[1])
                        except ValueError:
                            continue
            if result:
                return result
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


class _WSReceiveProxy:
    """Proxy only the async iterator so the underlying websocket stays untouched."""

    def __init__(self, websocket: Any, adapter: Any) -> None:
        self._websocket = websocket
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._websocket, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        started = time.perf_counter()
        # IMPORTANT: this timestamp is taken after __anext__ returns, i.e. at the
        # first point at which application code actually received the message.
        message = await self._websocket.__anext__()
        received_at_ms = time.time() * 1000
        wait_ms = (time.perf_counter() - started) * 1000

        stats = getattr(
            self._adapter,
            "_e2e_ws_stats",
            {"messages": 0, "bytes": 0, "source_age_total_ms": 0.0, "source_age_max_ms": 0.0},
        )
        self._adapter._e2e_ws_stats = stats
        stats["messages"] += 1
        size = len(message) if isinstance(message, (str, bytes)) else 0
        stats["bytes"] += size

        stream = ""
        event_ms = None
        trace_id = None
        if isinstance(message, bytes):
            try:
                message_text = message.decode("utf-8")
            except UnicodeDecodeError:
                message_text = ""
        else:
            message_text = message if isinstance(message, str) else ""
        if message_text:
            try:
                envelope = json.loads(message_text)
                stream = str(envelope.get("stream", ""))
                data = envelope.get("data", envelope)
                if isinstance(data, dict):
                    event_ms = data.get("T", data.get("E"))
                trace_id = _trace_id(envelope, stream)
            except (TypeError, ValueError):
                pass

        source_age_ms = None
        event_number = _numeric(event_ms)
        if event_number is not None:
            source_age_ms = max(0.0, received_at_ms - event_number)
            stats["source_age_total_ms"] += source_age_ms
            stats["source_age_max_ms"] = max(stats["source_age_max_ms"], source_age_ms)

        sample = {
            "trace_id": trace_id,
            "stream": stream,
            "exchange_event_ms": event_number,
            "received_at_ms": round(received_at_ms, 3),
            "source_age_ms": round(source_age_ms, 3) if source_age_ms is not None else None,
            "receive_wait_ms": round(wait_ms, 3),
            "bytes": size,
        }
        recent = getattr(self._adapter, "_e2e_ws_recent", None)
        if recent is None:
            recent = deque(maxlen=50)
            self._adapter._e2e_ws_recent = recent
        recent.append(sample)

        # DEBUG is emitted for every message when debug logging is enabled. WARN
        # is reserved for actionable lag so telemetry itself does not flood logs.
        _logger().debug("market-data websocket receive", extra={"aitos_extra": sample})
        if source_age_ms is not None and source_age_ms >= 1000:
            _logger().warning("market-data websocket receive lag", extra={"aitos_extra": sample})
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


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_binance()
    _install_eventbus()
    _install_live_state()


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
        self._e2e_ws_stats = {
            "messages": 0,
            "bytes": 0,
            "source_age_total_ms": 0.0,
            "source_age_max_ms": 0.0,
        }
        self._e2e_ws_recent = deque(maxlen=50)

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
        self._e2e_redis_stats = {
            "count": 0,
            "total_ms": 0.0,
            "max_ms": 0.0,
            "min_ms": None,
            "timeouts": 0,
        }
        self._e2e_redis_recent = deque(maxlen=50)
        redis = getattr(self, "_redis", None)
        original_xadd = getattr(redis, "xadd", None)
        if redis is None or original_xadd is None:
            return

        async def traced_xadd(*xargs: Any, **xkwargs: Any):
            started = time.perf_counter()
            stream = str(xargs[0]) if xargs else str(xkwargs.get("name", ""))
            try:
                return await original_xadd(*xargs, **xkwargs)
            except Exception as exc:
                if type(exc).__name__ in {"TimeoutError", "ConnectionError", "BusyLoadingError"}:
                    self._e2e_redis_stats["timeouts"] += 1
                raise
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                stats = self._e2e_redis_stats
                stats["count"] += 1
                stats["total_ms"] += elapsed
                stats["max_ms"] = max(stats["max_ms"], elapsed)
                if stats["min_ms"] is None:
                    stats["min_ms"] = elapsed
                else:
                    stats["min_ms"] = min(stats["min_ms"], elapsed)
                sample = {
                    "stage": "redis_xadd",
                    "stream": stream,
                    "latency_ms": round(elapsed, 3),
                    "at_ms": round(time.time() * 1000, 3),
                }
                self._e2e_redis_recent.append(sample)
                _logger().debug("redis xadd", extra={"aitos_extra": sample})
                if elapsed >= 100:
                    sample["cgroup_cpu"] = _cgroup_stats()
                    sample["cgroup_memory"] = _cgroup_memory()
                    _logger().warning("redis xadd latency", extra={"aitos_extra": sample})

        # redis-py's async client exposes xadd as an ordinary instance attribute;
        # keep the original bound method in the closure so restoration is safe.
        redis.xadd = traced_xadd

    @wraps(original_health)
    async def health(self: Any, *args: Any, **kwargs: Any):
        from dataclasses import replace

        status = await original_health(self, *args, **kwargs)
        try:
            details = dict(status.details)
            stats = dict(getattr(self, "_e2e_redis_stats", {}))
            count = stats.get("count", 0)
            stats["avg_ms"] = round(stats.get("total_ms", 0.0) / count, 3) if count else 0.0
            stats["total_ms"] = round(stats.get("total_ms", 0.0), 3)
            stats["max_ms"] = round(stats.get("max_ms", 0.0), 3)
            if stats.get("min_ms") is not None:
                stats["min_ms"] = round(stats["min_ms"], 3)
            stats["cgroup_cpu"] = _cgroup_stats()
            stats["cgroup_memory"] = _cgroup_memory()
            stats["recent"] = list(getattr(self, "_e2e_redis_recent", ()))
            details.setdefault("market_data_e2e", {})["redis_xadd"] = stats
            return replace(status, details=details)
        except Exception:
            return status

    EventBus.__init__ = init
    EventBus.health_check = health


def _install_live_state() -> None:
    """Time the CPU-side state update where every accepted TradeTick enters."""
    try:
        from aitos.intelligence.live_state import LiveMarketStateStore
    except Exception:
        return
    cls = LiveMarketStateStore
    if getattr(cls, "_e2e_on_trade_installed", False):
        return
    cls._e2e_on_trade_installed = True
    original = cls.on_trade

    @wraps(original)
    def on_trade(self: Any, trade: Any, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        result = original(self, trade, *args, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        symbol = str(getattr(trade, "symbol", ""))
        trade_id = getattr(trade, "trade_id", None)
        sample = {
            "stage": "live_state_on_trade",
            "symbol": symbol,
            "trade_id": trade_id,
            "duration_ms": round(elapsed, 3),
            "trade_timestamp": getattr(getattr(trade, "timestamp", None), "isoformat", lambda: None)(),
            "at_ms": round(time.time() * 1000, 3),
        }
        stats = getattr(self, "_e2e_on_trade_stats", None)
        if stats is None:
            stats = defaultdict(lambda: {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
            self._e2e_on_trade_stats = stats
        _record(stats, symbol or "unknown", elapsed)
        _logger().debug("live state trade processing", extra={"aitos_extra": sample})
        if elapsed >= 10:
            _logger().warning("live state trade processing latency", extra={"aitos_extra": sample})
        return result

    cls.on_trade = on_trade


async def _watchdog(eventbus: Any) -> None:
    """Record scheduler stalls; a large value means application code did not yield."""
    loop = asyncio.get_running_loop()
    interval = 1.0
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        lag = max(0.0, (loop.time() - expected) * 1000)
        eventbus._e2e_loop_samples = getattr(eventbus, "_e2e_loop_samples", 0) + 1
        eventbus._e2e_loop_lag_ms = lag
        eventbus._e2e_loop_lag_max_ms = max(
            getattr(eventbus, "_e2e_loop_lag_max_ms", 0.0), lag
        )
        if lag >= 100:
            _logger().warning(
                "event-loop scheduling lag",
                extra={
                    "aitos_extra": {
                        "stage": "event_loop",
                        "lag_ms": round(lag, 3),
                        "cgroup_cpu": _cgroup_stats(),
                    }
                },
            )


__all__ = ["install"]
