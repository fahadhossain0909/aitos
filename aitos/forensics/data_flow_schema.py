"""Canonical data-flow observability schema for AITOS market pipeline.

Phase 1 of the Data Flow Observability plan consolidates scattered telemetry
into one consistent vocabulary so stages can be correlated end-to-end without
OpenTelemetry (yet).

Stages mirror the live path:
  transport → receive → decode → normalize → gateway_queue → redis_publish →
  redis_consumer → live_state → feature → scanner → trade_lifecycle
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PipelineStage(str, Enum):
    TRANSPORT = "transport"
    RECEIVE = "receive"
    DECODE = "decode"
    NORMALIZE = "normalize"
    GATEWAY_QUEUE = "gateway_queue"
    REDIS_PUBLISH = "redis_publish"
    REDIS_CONSUMER = "redis_consumer"
    LIVE_STATE = "live_state"
    FEATURE = "feature"
    SCANNER = "scanner"
    TRADE_LIFECYCLE = "trade_lifecycle"
    PERSISTENCE = "persistence"


PIPELINE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.TRANSPORT,
    PipelineStage.RECEIVE,
    PipelineStage.DECODE,
    PipelineStage.NORMALIZE,
    PipelineStage.GATEWAY_QUEUE,
    PipelineStage.REDIS_PUBLISH,
    PipelineStage.REDIS_CONSUMER,
    PipelineStage.LIVE_STATE,
    PipelineStage.FEATURE,
    PipelineStage.SCANNER,
    PipelineStage.TRADE_LIFECYCLE,
)


class DataIssueClass(str, Enum):
    """Separate late/valid data from corrupted data (plan section 26)."""

    STALE = "stale"
    DUPLICATE = "duplicate"
    SCHEMA_INVALID = "schema_invalid"
    SEMANTIC_INVALID = "semantic_invalid"
    SEQUENCE_GAP = "sequence_gap"
    DECODE_ERROR = "decode_error"
    DROPPED_BACKPRESSURE = "dropped_backpressure"
    PROCESSING_TIMEOUT = "processing_timeout"
    PROCESSING_ERROR = "processing_error"
    NONE = "none"


def make_trace_id(symbol: str, event_id: Any) -> str | None:
    """Canonical market-data trace identity: ``md:{SYMBOL}:{event_id}``."""
    symbol_n = str(symbol or "").strip().upper()
    if not symbol_n or event_id is None:
        return None
    return f"md:{symbol_n}:{event_id}"


def parse_trace_id(trace_id: str) -> tuple[str, str] | None:
    if not trace_id or not trace_id.startswith("md:"):
        return None
    parts = trace_id.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


@dataclass
class StageTimestamps:
    """Per-event stage clock (milliseconds since epoch where known)."""

    source_timestamp_ms: float | None = None
    received_at_ms: float | None = None
    decoded_at_ms: float | None = None
    normalized_at_ms: float | None = None
    queue_enter_ms: float | None = None
    queue_exit_ms: float | None = None
    redis_publish_ms: float | None = None
    consumer_receive_ms: float | None = None
    state_update_ms: float | None = None
    feature_ready_ms: float | None = None
    scanner_evaluated_ms: float | None = None

    def latency_ms(self, start: str, end: str) -> float | None:
        a = getattr(self, start, None)
        b = getattr(self, end, None)
        if a is None or b is None:
            return None
        return max(0.0, float(b) - float(a))

    def to_dict(self) -> dict[str, float | None]:
        return asdict(self)


@dataclass
class StageSnapshot:
    stage: str
    input_count: int = 0
    output_count: int = 0
    error_count: int = 0
    drop_count: int = 0
    queue_depth: int = 0
    oldest_event_age_ms: float | None = None
    source_age_ms: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    latency_max_ms: float | None = None
    input_rate: float = 0.0
    output_rate: float = 0.0
    issues: dict[str, int] = field(default_factory=dict)
    pressure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "PIPELINE_ORDER",
    "DataIssueClass",
    "PipelineStage",
    "StageSnapshot",
    "StageTimestamps",
    "make_trace_id",
    "parse_trace_id",
]
