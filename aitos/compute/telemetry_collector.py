"""Bounded in-process telemetry windows for per-symbol compute estimation."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from math import ceil, isfinite
from threading import RLock
from time import monotonic
from typing import Callable

from aitos.compute.workload_estimator import SymbolTelemetry


@dataclass
class _SymbolWindow:
    events: deque[float]
    depths: deque[tuple[float, int]]
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

    Process-local and side-effect free. The registry is LRU-bounded, each
    metric uses a bounded deque, and samples expire outside the rolling window.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        max_symbols: int = 5000,
        max_samples_per_metric: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            not isfinite(window_seconds)
            or window_seconds < 1
            or isinstance(max_symbols, bool)
            or not isinstance(max_symbols, int)
            or max_symbols < 1
            or isinstance(max_samples_per_metric, bool)
            or not isinstance(max_samples_per_metric, int)
            or max_samples_per_metric < 1
        ):
            raise ValueError("telemetry collector bounds must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.window_seconds = float(window_seconds)
        self.max_symbols = max_symbols
        self.max_samples_per_metric = max_samples_per_metric
        self._clock = clock
        self._windows: OrderedDict[str, _SymbolWindow] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")
        key = symbol.strip().upper()
        if not key:
            raise ValueError("symbol cannot be empty")
        return key

    def _timestamp(self, timestamp: float | None) -> float:
        now = self._clock() if timestamp is None else float(timestamp)
        if not isfinite(now) or now < 0:
            raise ValueError("timestamp must be finite and non-negative")
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
        window.last_seen = max(window.last_seen, now)
        self._windows.move_to_end(key)
        while len(self._windows) > self.max_symbols:
            self._windows.popitem(last=False)
        return window

    @staticmethod
    def _ensure_monotonic(samples: deque, now: float, metric: str) -> None:
        if samples:
            last = samples[-1][0] if metric == "depth" else samples[-1]
            if now < last:
                raise ValueError(f"{metric} timestamps must be monotonic per symbol")

    def _prune(self, window: _SymbolWindow, cutoff: float) -> None:
        for samples in (window.events, window.analyses):
            while samples and samples[0] < cutoff:
                samples.popleft()
        while window.depths and window.depths[0][0] < cutoff:
            window.depths.popleft()

    def record_event(self, symbol: str, *, timestamp: float | None = None) -> None:
        now = self._timestamp(timestamp)
        with self._lock:
            window = self._window(symbol, now)
            self._ensure_monotonic(window.events, now, "event")
            self._prune(window, now - self.window_seconds)
            window.events.append(now)

    def record_book_depth(
        self, symbol: str, depth_levels: int, *, timestamp: float | None = None
    ) -> None:
        if (
            isinstance(depth_levels, bool)
            or not isinstance(depth_levels, int)
            or depth_levels < 0
        ):
            raise ValueError("depth_levels must be a non-negative integer")
        now = self._timestamp(timestamp)
        with self._lock:
            window = self._window(symbol, now)
            self._ensure_monotonic(window.depths, now, "depth")
            self._prune(window, now - self.window_seconds)
            window.depths.append((now, depth_levels))

    def record_analysis(self, symbol: str, *, timestamp: float | None = None) -> None:
        now = self._timestamp(timestamp)
        with self._lock:
            window = self._window(symbol, now)
            self._ensure_monotonic(window.analyses, now, "analysis")
            self._prune(window, now - self.window_seconds)
            window.analyses.append(now)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1))
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
            cutoff = current - self.window_seconds
            self._prune(window, cutoff)
            self._windows.move_to_end(key)
            bucket_count = max(1, ceil(self.window_seconds))
            buckets = [0] * bucket_count
            for event_time in window.events:
                age = current - event_time
                if 0 <= age < self.window_seconds:
                    bucket = min(bucket_count - 1, int(age))
                    buckets[bucket] += 1
            event_rate = self._percentile([float(value) for value in buckets], 0.95)
            analysis_count = sum(
                1 for value in window.analyses if cutoff <= value <= current
            )
            depth_values = [
                float(value)
                for timestamp, value in window.depths
                if cutoff <= timestamp <= current
            ]
            depth = int(self._percentile(depth_values, 0.95))
            sample_count = min(
                len(window.events), len(window.depths), len(window.analyses)
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
                    len(window.events) for window in self._windows.values()
                ),
                retained_depth_samples=sum(
                    len(window.depths) for window in self._windows.values()
                ),
                retained_analysis_samples=sum(
                    len(window.analyses) for window in self._windows.values()
                ),
            )
