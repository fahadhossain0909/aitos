"""Root-cause telemetry for the canonical market-data pipeline.

Observational only: this module does not change queueing, retry, ordering, or
freshness policy. It measures the boundaries needed to distinguish Redis
latency, ClickHouse latency, persistence backlog, event-loop starvation, and
WebSocket watchdog reconnects.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any

_INSTALLED = False
_LOGGER_NAME = "aitos.forensics.root_cause"


def _logger():
    from aitos.logging_setup import get_logger

    return get_logger(_LOGGER_NAME)


def _bucket() -> dict[str, Any]:
    return {
        "count": 0,
        "total_ms": 0.0,
        "max_ms": 0.0,
        "min_ms": None,
        "slow_over_100ms": 0,
        "slow_over_1000ms": 0,
    }


def _record(stats: dict[str, Any], elapsed_ms: float) -> None:
    stats["count"] += 1
    stats["total_ms"] += elapsed_ms
    stats["max_ms"] = max(stats["max_ms"], elapsed_ms)
    stats["min_ms"] = (
        elapsed_ms if stats["min_ms"] is None else min(stats["min_ms"], elapsed_ms)
    )
    if elapsed_ms >= 100:
        stats["slow_over_100ms"] += 1
    if elapsed_ms >= 1000:
        stats["slow_over_1000ms"] += 1


def _format(stats: dict[str, Any]) -> dict[str, Any]:
    out = dict(stats)
    count = int(out.get("count", 0))
    out["avg_ms"] = round(float(out.get("total_ms", 0.0)) / count, 3) if count else 0.0
    out["total_ms"] = round(float(out.get("total_ms", 0.0)), 3)
    out["max_ms"] = round(float(out.get("max_ms", 0.0)), 3)
    if out.get("min_ms") is not None:
        out["min_ms"] = round(float(out["min_ms"]), 3)
    return out


def _queue_depth(self: Any) -> int:
    queue = getattr(self, "_queue", None)
    qsize = getattr(queue, "qsize", None)
    if qsize is None:
        return 0
    try:
        return int(qsize())
    except Exception:
        return 0


def _ensure_gateway_state(self: Any) -> None:
    if not hasattr(self, "_root_cause_drain_stats"):
        self._root_cause_drain_stats = _bucket()
    if not hasattr(self, "_root_cause_drain_stages"):
        self._root_cause_drain_stages = defaultdict(_bucket)
    if not hasattr(self, "_root_cause_recent"):
        self._root_cause_recent = deque(maxlen=100)
    if not hasattr(self, "_root_cause_last_accept_monotonic"):
        self._root_cause_last_accept_monotonic = None


def _ensure_repository_state(self: Any) -> None:
    if not hasattr(self, "_root_cause_ch_stats"):
        self._root_cause_ch_stats = defaultdict(_bucket)
    if not hasattr(self, "_root_cause_ch_recent"):
        self._root_cause_ch_recent = deque(maxlen=100)


def _ensure_persistence_state(self: Any) -> None:
    if not hasattr(self, "_root_cause_enqueued_at"):
        self._root_cause_enqueued_at = {}
    if not hasattr(self, "_root_cause_persist_wait"):
        self._root_cause_persist_wait = defaultdict(_bucket)
    if not hasattr(self, "_root_cause_persist_recent"):
        self._root_cause_persist_recent = deque(maxlen=100)


def install() -> None:
    """Install once; telemetry failures must never break application startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_gateway()
    _install_repository()
    _install_persistence_sink()


def _install_gateway() -> None:
    try:
        from aitos.market_data.gateway import MarketDataGateway
    except Exception:
        return
    cls = MarketDataGateway
    if getattr(cls, "_root_cause_telemetry_installed", False):
        return
    cls._root_cause_telemetry_installed = True
    original_init = cls.__init__
    original_accept = cls.accept_async
    original_drain = cls.drain_once
    original_snapshot = cls.snapshot

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._root_cause_drain_stats = _bucket()
        self._root_cause_drain_stages = defaultdict(_bucket)
        self._root_cause_recent = deque(maxlen=100)
        self._root_cause_last_accept_monotonic = None

    async def accept_async(self: Any, event: Any) -> bool:
        _ensure_gateway_state(self)
        accepted = await original_accept(self, event)
        now = time.monotonic()
        previous = self._root_cause_last_accept_monotonic
        if previous is not None:
            gap_ms = max(0.0, (now - previous) * 1000)
            if gap_ms >= 1000:
                self._root_cause_recent.append(
                    {
                        "stage": "gateway_receive_gap",
                        "gap_ms": round(gap_ms, 3),
                        "event_type": getattr(
                            getattr(event, "event_type", None), "value", None
                        ),
                        "symbol": getattr(event, "symbol", None),
                        "at_ms": round(time.time() * 1000, 3),
                    }
                )
        if accepted:
            self._root_cause_last_accept_monotonic = now
        return accepted

    async def drain_once(self: Any) -> bool:
        _ensure_gateway_state(self)
        started = time.perf_counter()
        queue_before = self.queue.qsize()
        try:
            return await original_drain(self)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            _record(self._root_cause_drain_stats, elapsed)
            self._root_cause_recent.append(
                {
                    "stage": "gateway_drain",
                    "duration_ms": round(elapsed, 3),
                    "queue_before": queue_before,
                    "queue_after": self.queue.qsize(),
                    "at_ms": round(time.time() * 1000, 3),
                }
            )

    def record_drain_stage(self: Any, stage: str, elapsed_ms: float) -> None:
        _ensure_gateway_state(self)
        _record(self._root_cause_drain_stages[str(stage)], max(0.0, float(elapsed_ms)))

    def snapshot(self: Any) -> dict[str, object]:
        _ensure_gateway_state(self)
        result = original_snapshot(self)
        result["root_cause_telemetry"] = {
            "gateway_drain": _format(self._root_cause_drain_stats),
            "gateway_drain_stages": {
                key: _format(value)
                for key, value in sorted(self._root_cause_drain_stages.items())
            },
            "recent": list(self._root_cause_recent),
            "last_accept_monotonic": self._root_cause_last_accept_monotonic,
        }
        return result

    cls.__init__ = init
    cls.accept_async = accept_async
    cls.drain_once = drain_once
    cls.record_drain_stage = record_drain_stage
    cls.snapshot = snapshot


def _install_repository() -> None:
    try:
        from aitos.data.repository import MarketDataRepository
    except Exception:
        return
    cls = MarketDataRepository
    if getattr(cls, "_root_cause_telemetry_installed", False):
        return
    cls._root_cause_telemetry_installed = True
    original_init = cls.__init__
    original_health = cls.health_check

    methods = (
        "save_trade_tick",
        "save_order_book_snapshot",
        "save_kline",
        "save_funding_rate",
        "save_open_interest",
        "save_market_event",
    )

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._root_cause_ch_stats = defaultdict(_bucket)
        self._root_cause_ch_recent = deque(maxlen=100)

    def wrap_method(name: str, original: Any):
        @wraps(original)
        async def wrapped(self: Any, *args: Any, **kwargs: Any):
            _ensure_repository_state(self)
            started = time.perf_counter()
            event = args[0] if args else None
            symbol = getattr(event, "symbol", None)
            try:
                return await original(self, *args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                key = f"{name}:{str(symbol or 'unknown').upper()}"
                _record(self._root_cause_ch_stats[key], elapsed)
                self._root_cause_ch_recent.append(
                    {
                        "stage": "clickhouse_write",
                        "operation": name,
                        "symbol": symbol,
                        "latency_ms": round(elapsed, 3),
                        "at_ms": round(time.time() * 1000, 3),
                    }
                )
                if elapsed >= 100:
                    _logger().warning(
                        "clickhouse write latency",
                        extra={"aitos_extra": self._root_cause_ch_recent[-1]},
                    )

        return wrapped

    @wraps(original_health)
    async def health(self: Any, *args: Any, **kwargs: Any):
        _ensure_repository_state(self)
        from dataclasses import replace

        status = await original_health(self, *args, **kwargs)
        details = dict(status.details)
        details["root_cause_telemetry"] = {
            "clickhouse_writes": {
                key: _format(value)
                for key, value in sorted(self._root_cause_ch_stats.items())
            },
            "recent": list(self._root_cause_ch_recent),
        }
        return replace(status, details=details)

    cls.__init__ = init
    cls.health_check = health
    for name in methods:
        original = getattr(cls, name, None)
        if original is not None:
            setattr(cls, name, wrap_method(name, original))


def _install_persistence_sink() -> None:
    try:
        from aitos.market_data.persistence_sink import (
            CanonicalMarketDataPersistenceSink,
        )
    except Exception:
        return
    cls = CanonicalMarketDataPersistenceSink
    if getattr(cls, "_root_cause_telemetry_installed", False):
        return
    cls._root_cause_telemetry_installed = True
    original_init = cls.__init__
    original_enqueue = getattr(cls, "_enqueue", None)
    original_persist_batch = getattr(cls, "_persist_batch", None)
    original_snapshot = cls.snapshot

    if original_enqueue is None or original_persist_batch is None:
        cls._root_cause_telemetry_installed = False
        return

    @wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._root_cause_enqueued_at = {}
        self._root_cause_persist_wait = defaultdict(_bucket)
        self._root_cause_persist_recent = deque(maxlen=100)

    async def enqueue(self: Any, event: Any) -> None:
        _ensure_persistence_state(self)
        before = _queue_depth(self)
        await original_enqueue(self, event)
        after = _queue_depth(self)
        if after > before:
            event_id = str(getattr(event, "event_id", id(event)))
            self._root_cause_enqueued_at[event_id] = time.monotonic()
            self._root_cause_persist_recent.append(
                {
                    "stage": "persistence_enqueue",
                    "event_type": getattr(
                        getattr(event, "event_type", None), "value", None
                    ),
                    "symbol": getattr(event, "symbol", None),
                    "queue_depth": after,
                    "at_ms": round(time.time() * 1000, 3),
                }
            )

    async def persist_batch(self: Any, events: list[Any]) -> Any:
        _ensure_persistence_state(self)
        started = time.perf_counter()
        now = time.monotonic()
        batch = list(events)
        event_types = defaultdict(int)
        symbols: list[str] = []
        for event in batch:
            event_id = str(getattr(event, "event_id", id(event)))
            enqueued = self._root_cause_enqueued_at.pop(event_id, None)
            if enqueued is not None:
                event_type = str(
                    getattr(getattr(event, "event_type", None), "value", "unknown")
                )
                _record(
                    self._root_cause_persist_wait[event_type],
                    max(0.0, (now - enqueued) * 1000),
                )
            event_type = str(
                getattr(getattr(event, "event_type", None), "value", "unknown")
            )
            event_types[event_type] += 1
            symbol = getattr(event, "symbol", None)
            if symbol:
                symbols.append(str(symbol))
        try:
            return await original_persist_batch(self, batch)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            sample = {
                "stage": "persistence_batch_write",
                "batch_size": len(batch),
                "event_types": dict(event_types),
                "symbols": sorted(set(symbols)),
                "write_latency_ms": round(elapsed, 3),
                "queue_depth": _queue_depth(self),
                "at_ms": round(time.time() * 1000, 3),
            }
            self._root_cause_persist_recent.append(sample)
            if elapsed >= 100:
                _logger().warning(
                    "persistence batch write latency",
                    extra={"aitos_extra": sample},
                )

    def snapshot(self: Any) -> dict[str, object]:
        _ensure_persistence_state(self)
        result = original_snapshot(self)
        result["root_cause_telemetry"] = {
            "queue_wait": {
                key: _format(value)
                for key, value in sorted(self._root_cause_persist_wait.items())
            },
            "recent": list(self._root_cause_persist_recent),
            "tracked_enqueued_events": len(self._root_cause_enqueued_at),
        }
        return result

    cls.__init__ = init
    cls._enqueue = enqueue
    cls._persist_batch = persist_batch
    cls.snapshot = snapshot


install()
