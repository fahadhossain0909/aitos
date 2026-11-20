"""Bounded in-process telemetry windows for per-symbol compute estimation."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from math import ceil, isfinite
from threading import RLock
from time import monotonic

from aitos.compute.workload_estimator import SymbolTelemetry


@dataclass
class _SymbolWindow:
    events: deque[float]
    depths: deque[int]
    analyses: deque[float]
    last_seen: float


@dataclass(frozen=True)
class TelemetryCollectorStats:
    tracked_symbols: int
    retained_event_samples: int
    retained_depth_samples: int
    retained_analysis_samples: int


class SymbolTelemetryCollector:
    """Collect bounded monotonic-time samples and export estimator inputs.

    This collector is process-local and intentionally has no persistence or
    network side effects. A durable telemetry pipeline can feed the same
    SymbolTelemetry contract later. Old samples are pruned on writes/snapshot;
    the per-symbol registry is LRU-bounded to prevent unbounded cardinality.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        max_symbols: int = 5000,
        max_samples_per_metric: int = 4096,
        clock=monotonic,
    ) -> None:
        if window_seconds <= 0 or max_symbols < 1 or max_samples_per_metric < 1:
            raise ValueError("telemetry collector bounds must be positive")
        self.window_seconds = float(window_seconds)
        self.max_symbols = int(max_symbols)
        self.max_samples_per_metric = int(max_samples_per_metric)
        self._clock = clock
        self._windows: OrderedDict[str, _SymbolWindow] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        key = symbol.strip().upper()
        if not key:
            raise ValueError("symbol cannot be empty")
        return key

    def _timestamp(self, timestamp: float | None) -> float:
        now = self._timestamp(timestamp)
        return now

    def _window(self, symbol: str, now: float) -> _SymbolWindow:
        key = self._symbol_key(symbol)
        window = self._windows.get(key)
        if window is None:
            window = _SymbolWindow(
                events=deque(maxlen=self.max_samples_per_metric),
                depths=deque(maxlen=self.max_samples_per_metric),
                analyses=deque(maxlen=self.max_samples_per_metric),
                last_seen=now,
            )
            self._windows[key] = window
        window.last_seen = now
        self._windows.move_to_end(key)
        while len(self._windows) > self.max_symbols:
            self._windows.popitem(last=False)
        return window

    def _prune(self, window: _SymbolWindow, cutoff: float) -> None:
        for samples in (window.events, window.analyses):
            while samples and samples[0] < cutoff:
                samples.popleft()

    def record_event(self, symbol: str, *, timestamp: float | None = None) -> None:
        now = self._clock() if timestamp is None else float(timestamp)
        if not isfinite(now) or now < 0:
            raise ValueError("timestamp must be finite and non-negative")
        with self._lock:
            window = self._window(symbol, now)
            self._prune(window, now - self.window_seconds)
            window.events.append(now)

    def record_book_depth(
        self, symbol: str, depth_levels: int, *, timestamp: float | None = None
    ) -> None:
        if depth_levels < 0:
            raise ValueError("depth_levels must be non-negative")
        now = self._timestamp(timestamp)
        with self._lock:
            window = self._window(symbol, now)
            self._prune(window, now - self.window_seconds)
            window.depths.append(int(depth_levels))

    def record_analysis(self, symbol: str, *, timestamp: float | None = None) -> None:
        now = self._clock() if timestamp is None else float(timestamp)
        with self._lock:
            window = self._window(symbol, now)
            self._prune(window, now - self.window_seconds)
            window.analyses.append(now)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(
            0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1)
        )
        return float(ordered[index])

    def snapshot(
        self, symbol: str, *, now: float | None = None
    ) -> SymbolTelemetry | None:
        current = self._timestamp(now)
        key = self._symbol_key(symbol)
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                return None
            self._prune(window, current - self.window_seconds)
            self._windows.move_to_end(key)
            bucket_count = max(1, ceil(self.window_seconds))
            buckets = [0] * bucket_count
            cutoff = current - self.window_seconds
            for event_time in window.events:
                age = current - event_time
                if 0 <= age < self.window_seconds:
                    bucket = min(bucket_count - 1, int(age))
                    buckets[bucket] += 1
            event_rate = self._percentile([float(v) for v in buckets], 0.95)
            analysis_count = sum(
                1
                for value in window.analyses
                if cutoff <= value <= current
            )
            depth = int(self._percentile([float(v) for v in window.depths], 0.95))
            sample_count = (
                len(window.events) + len(window.depths) + len(window.analyses)
            )
            return SymbolTelemetry(
                symbol=key,
                p95_event_rate_per_second=event_rate,
                p95_book_depth_levels=depth,
                analysis_runs_per_minute=(
                    analysis_count * 60.0 / self.window_seconds
                ),
                sample_count=sample_count,
            )

    def stats(self) -> TelemetryCollectorStats:
        with self._lock:
            return TelemetryCollectorStats(
                tracked_symbols=len(self._windows),
                retained_event_samples=sum(
                    len(w.events) for w in self._windows.values()
                ),
                retained_depth_samples=sum(len(w.depths) for w in self._windows.values()),
                retained_analysis_samples=sum(
                    len(w.analyses) for w in self._windows.values()
                ),
            )
