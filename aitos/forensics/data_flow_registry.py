"""In-process stage metrics registry with percentile latency tracking.

Consolidates component-level counters into one place so /data-flow and
Prometheus exporters can answer: input vs output rate, queue age, and
where pressure builds (input_rate > output_rate with rising age).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from aitos.forensics.data_flow_schema import (
    PIPELINE_ORDER,
    DataIssueClass,
    PipelineStage,
    StageSnapshot,
    make_trace_id,
)


def _percentile(samples: list[float], pct: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


class _StageBucket:
    __slots__ = (
        "input_count",
        "output_count",
        "error_count",
        "drop_count",
        "queue_depth",
        "oldest_event_age_ms",
        "source_age_ms",
        "latencies_ms",
        "issues",
        "window_inputs",
        "window_outputs",
    )

    def __init__(self) -> None:
        self.input_count = 0
        self.output_count = 0
        self.error_count = 0
        self.drop_count = 0
        self.queue_depth = 0
        self.oldest_event_age_ms: float | None = None
        self.source_age_ms: float | None = None
        self.latencies_ms: deque[float] = deque(maxlen=2048)
        self.issues: dict[str, int] = defaultdict(int)
        self.window_inputs: deque[tuple[float, int]] = deque(maxlen=256)
        self.window_outputs: deque[tuple[float, int]] = deque(maxlen=256)


class DataFlowRegistry:
    """Process-wide registry keyed by (stage, symbol). symbol='' = aggregate."""

    def __init__(self, rate_window_seconds: float = 10.0) -> None:
        self._lock = threading.RLock()
        self._buckets: dict[tuple[str, str], _StageBucket] = {}
        self._rate_window = float(rate_window_seconds)
        self._recent_traces: deque[dict[str, Any]] = deque(maxlen=200)

    def _bucket(self, stage: str | PipelineStage, symbol: str = "") -> _StageBucket:
        key = (str(stage), str(symbol or "").upper())
        if key not in self._buckets:
            self._buckets[key] = _StageBucket()
        return self._buckets[key]

    def record_input(
        self,
        stage: str | PipelineStage,
        *,
        symbol: str = "",
        n: int = 1,
        source_age_ms: float | None = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            b = self._bucket(stage, symbol)
            b.input_count += n
            b.window_inputs.append((now, n))
            if source_age_ms is not None:
                b.source_age_ms = float(source_age_ms)
            if symbol:
                agg = self._bucket(stage, "")
                agg.input_count += n
                agg.window_inputs.append((now, n))

    def record_output(
        self,
        stage: str | PipelineStage,
        *,
        symbol: str = "",
        n: int = 1,
        latency_ms: float | None = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            b = self._bucket(stage, symbol)
            b.output_count += n
            b.window_outputs.append((now, n))
            if latency_ms is not None:
                b.latencies_ms.append(float(latency_ms))
            if symbol:
                agg = self._bucket(stage, "")
                agg.output_count += n
                agg.window_outputs.append((now, n))
                if latency_ms is not None:
                    agg.latencies_ms.append(float(latency_ms))

    def record_error(
        self,
        stage: str | PipelineStage,
        *,
        symbol: str = "",
        issue: DataIssueClass | str = DataIssueClass.NONE,
        n: int = 1,
    ) -> None:
        issue_key = str(issue.value if isinstance(issue, DataIssueClass) else issue)
        with self._lock:
            b = self._bucket(stage, symbol)
            b.error_count += n
            b.issues[issue_key] += n
            if symbol:
                agg = self._bucket(stage, "")
                agg.error_count += n
                agg.issues[issue_key] += n

    def record_drop(
        self,
        stage: str | PipelineStage,
        *,
        symbol: str = "",
        issue: DataIssueClass | str = DataIssueClass.DROPPED_BACKPRESSURE,
        n: int = 1,
    ) -> None:
        issue_key = str(issue.value if isinstance(issue, DataIssueClass) else issue)
        with self._lock:
            b = self._bucket(stage, symbol)
            b.drop_count += n
            b.issues[issue_key] += n
            if symbol:
                agg = self._bucket(stage, "")
                agg.drop_count += n
                agg.issues[issue_key] += n
    def set_queue(
        self,
        stage: str | PipelineStage,
        *,
        depth: int,
        oldest_event_age_ms: float | None = None,
        symbol: str = "",
    ) -> None:
        with self._lock:
            b = self._bucket(stage, symbol)
            b.queue_depth = int(depth)
            if oldest_event_age_ms is not None:
                b.oldest_event_age_ms = float(oldest_event_age_ms)

    def note_trace(
        self,
        *,
        trace_id: str | None,
        stage: str | PipelineStage,
        symbol: str = "",
        latency_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not trace_id:
            return
        sample = {
            "trace_id": trace_id,
            "stage": str(stage),
            "symbol": str(symbol or "").upper(),
            "latency_ms": latency_ms,
            "ts": time.time(),
        }
        if extra:
            sample.update(extra)
        with self._lock:
            self._recent_traces.append(sample)

    def _rate(self, window: deque[tuple[float, int]]) -> float:
        if not window:
            return 0.0
        now = time.monotonic()
        cutoff = now - self._rate_window
        total = 0
        for ts, n in window:
            if ts >= cutoff:
                total += n
        return total / self._rate_window if self._rate_window > 0 else 0.0

    def snapshot_stage(
        self, stage: str | PipelineStage, symbol: str = ""
    ) -> StageSnapshot:
        with self._lock:
            b = self._bucket(stage, symbol)
            samples = list(b.latencies_ms)
            in_rate = self._rate(b.window_inputs)
            out_rate = self._rate(b.window_outputs)
            pressure = in_rate > out_rate * 1.05 and (
                (b.oldest_event_age_ms or 0) > 50 or b.queue_depth > 0
            )
            return StageSnapshot(
                stage=str(stage),
                input_count=b.input_count,
                output_count=b.output_count,
                error_count=b.error_count,
                drop_count=b.drop_count,
                queue_depth=b.queue_depth,
                oldest_event_age_ms=b.oldest_event_age_ms,
                source_age_ms=b.source_age_ms,
                latency_p50_ms=_percentile(samples, 50),
                latency_p95_ms=_percentile(samples, 95),
                latency_p99_ms=_percentile(samples, 99),
                latency_max_ms=max(samples) if samples else None,
                input_rate=round(in_rate, 3),
                output_rate=round(out_rate, 3),
                issues=dict(b.issues),
                pressure=pressure,
            )

    def pipeline_snapshot(self, symbol: str = "") -> list[StageSnapshot]:
        return [self.snapshot_stage(stage, symbol) for stage in PIPELINE_ORDER]

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._recent_traces)
        return items[-limit:]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._recent_traces.clear()


_GLOBAL: DataFlowRegistry | None = None
_GLOBAL_LOCK = threading.Lock()


def get_data_flow_registry() -> DataFlowRegistry:
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = DataFlowRegistry()
        return _GLOBAL


__all__ = [
    "DataFlowRegistry",
    "get_data_flow_registry",
    "make_trace_id",
]
