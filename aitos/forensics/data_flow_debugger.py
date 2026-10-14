"""Data Flow Debugger — symbol/window snapshot + pressure-point detection.

Implements the diagnostic view from the observability plan:

  Exchange WS → Decoder → Queue → Redis → Consumer → Live State → Feature → Scanner

When a stage has input_rate > output_rate and rising age/lag, it is marked as
the likely pressure point.
"""

from __future__ import annotations

from typing import Any

from aitos.forensics.data_flow_registry import DataFlowRegistry, get_data_flow_registry
from aitos.forensics.data_flow_schema import PIPELINE_ORDER, StageSnapshot


def _format_stage(snap: StageSnapshot) -> dict[str, Any]:
    return {
        "stage": snap.stage,
        "received": snap.input_count,
        "emitted": snap.output_count,
        "errors": snap.error_count,
        "drops": snap.drop_count,
        "queue_depth": snap.queue_depth,
        "oldest_event_age_ms": snap.oldest_event_age_ms,
        "source_age_ms": snap.source_age_ms,
        "input_rate": snap.input_rate,
        "output_rate": snap.output_rate,
        "latency_ms": {
            "p50": snap.latency_p50_ms,
            "p95": snap.latency_p95_ms,
            "p99": snap.latency_p99_ms,
            "max": snap.latency_max_ms,
        },
        "issues": snap.issues,
        "pressure": snap.pressure,
    }


def find_pressure_point(stages: list[StageSnapshot]) -> dict[str, Any] | None:
    """First stage where input exceeds output and age/queue indicates backlog."""
    for snap in stages:
        lag = (snap.input_count or 0) - (snap.output_count or 0)
        rate_pressure = snap.input_rate > snap.output_rate * 1.05
        age_pressure = (snap.oldest_event_age_ms or 0) >= 100
        if snap.pressure or (lag > 0 and (rate_pressure or age_pressure)):
            return {
                "stage": snap.stage,
                "reason": (
                    f"input_rate={snap.input_rate}/s > output_rate={snap.output_rate}/s"
                    if rate_pressure
                    else f"backlog_lag={lag}, oldest_age_ms={snap.oldest_event_age_ms}"
                ),
                "input_count": snap.input_count,
                "output_count": snap.output_count,
                "lag": lag,
            }
    return None


def build_data_flow_report(
    symbol: str = "",
    *,
    registry: DataFlowRegistry | None = None,
) -> dict[str, Any]:
    """Build the debugger JSON for one symbol (or aggregate when empty)."""
    reg = registry or get_data_flow_registry()
    symbol_n = str(symbol or "").strip().upper()
    stages = reg.pipeline_snapshot(symbol_n)
    pressure = find_pressure_point(stages)
    return {
        "symbol": symbol_n or "*",
        "stages": [_format_stage(s) for s in stages],
        "pressure_point": pressure,
        "recent_traces": reg.recent_traces(limit=30, symbol=symbol_n),
        "pipeline_order": [s.value for s in PIPELINE_ORDER],
    }


def render_text_report(report: dict[str, Any]) -> str:
    """Human-readable summary similar to the plan's ASCII diagram."""
    symbol = report.get("symbol") or "*"
    lines = [f"{symbol} DATA FLOW", "─" * 36]
    for stage in report.get("stages", []):
        name = stage["stage"]
        lines.append(name)
        lines.append(f"  in={stage['received']}  out={stage['emitted']}")
        if stage.get("errors"):
            lines.append(f"  errors={stage['errors']}")
        if stage.get("drops"):
            lines.append(f"  drops={stage['drops']}")
        lat = stage.get("latency_ms") or {}
        if lat.get("p99") is not None:
            lines.append(f"  latency p99={lat['p99']}ms")
        if stage.get("pressure"):
            lines.append("  ← PRESSURE")
        lines.append("       ↓")
    if lines and lines[-1].strip() == "↓":
        lines.pop()
    pp = report.get("pressure_point")
    if pp:
        lines.append("")
        lines.append(f"Likely pressure point: {pp['stage']} ({pp['reason']})")
    else:
        lines.append("")
        lines.append("No pressure point detected.")
    return "\n".join(lines)


__all__ = [
    "build_data_flow_report",
    "find_pressure_point",
    "render_text_report",
]
