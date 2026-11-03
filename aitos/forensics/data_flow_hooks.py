"""Safe, observational hooks into DataFlowRegistry.

Every function swallows exceptions so instrumentation can never break
the market-data or trading path. Import cost is deferred until first use.
"""

from __future__ import annotations

from typing import Any


def _reg():
    from aitos.forensics.data_flow_registry import get_data_flow_registry

    return get_data_flow_registry()


def record_input(
    stage: str,
    *,
    symbol: str = "",
    n: int = 1,
    source_age_ms: float | None = None,
) -> None:
    try:
        _reg().record_input(stage, symbol=symbol, n=n, source_age_ms=source_age_ms)
    except Exception:
        pass


def record_output(
    stage: str,
    *,
    symbol: str = "",
    n: int = 1,
    latency_ms: float | None = None,
) -> None:
    try:
        _reg().record_output(stage, symbol=symbol, n=n, latency_ms=latency_ms)
    except Exception:
        pass


def record_error(
    stage: str,
    *,
    symbol: str = "",
    issue: str = "none",
    n: int = 1,
) -> None:
    try:
        _reg().record_error(stage, symbol=symbol, issue=issue, n=n)
    except Exception:
        pass


def record_drop(
    stage: str,
    *,
    symbol: str = "",
    issue: str = "dropped_backpressure",
    n: int = 1,
) -> None:
    try:
        _reg().record_drop(stage, symbol=symbol, issue=issue, n=n)
    except Exception:
        pass


def set_queue(
    stage: str,
    *,
    depth: int,
    oldest_event_age_ms: float | None = None,
    symbol: str = "",
) -> None:
    try:
        _reg().set_queue(
            stage,
            depth=depth,
            oldest_event_age_ms=oldest_event_age_ms,
            symbol=symbol,
        )
    except Exception:
        pass


def note_trace(
    *,
    trace_id: str | None,
    stage: str,
    symbol: str = "",
    latency_ms: float | None = None,
    extra: dict[str, Any] | None = None,
    event_id: str | None = None,
    parent_trace_id: str | None = None,
) -> None:
    try:
        _reg().note_trace(
            trace_id=trace_id,
            stage=stage,
            symbol=symbol,
            latency_ms=latency_ms,
            extra=extra,
            event_id=event_id,
            parent_trace_id=parent_trace_id,
        )
    except Exception:
        pass


__all__ = [
    "note_trace",
    "record_drop",
    "record_error",
    "record_input",
    "record_output",
    "set_queue",
]
