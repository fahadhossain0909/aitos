#!/usr/bin/env python3
"""Attribute cumulative E2E telemetry to the actual forensic window."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

COUNTER_KEYS = {
    "received_events",
    "accepted_events",
    "published_events",
    "dropped_events",
    "freshness_drops",
    "publish_errors",
    "decode_errors",
    "sequence_errors",
    "reconnect_count",
    "stream_idle_timeouts",
    "connect_attempts",
    "successful_handshakes",
    "close_count",
    "market_events_received",
    "restarts",
    "idle_timeouts",
    "errors",
    "events",
    "accepted",
    "messages",
    "bytes",
    "slow_over_100ms",
    "slow_over_1000ms",
    "over_100ms",
    "over_1000ms",
    "timeouts",
    "enqueued",
    "rejected",
    "batches",
    "batch_events",
}
LATENCY_TOTAL_KEYS = {"count", "total_ms"}


def load_samples(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("health"), dict):
            rows.append(row)
    return rows


def modules(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for module in health.get("modules", []):
        if not isinstance(module, dict):
            continue
        details = module.get("details")
        module_id = module.get("module_id")
        if module_id and isinstance(details, dict):
            result[str(module_id)] = details
    return result


def nested_get(root: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = root
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return value
    return None


def delta_tree(start: Any, end: Any) -> Any:
    if not isinstance(start, dict) or not isinstance(end, dict):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(set(start) | set(end)):
        a, b = start.get(key), end.get(key)
        if key in COUNTER_KEYS or key in LATENCY_TOTAL_KEYS:
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                result[key] = b - a
            continue
        child = delta_tree(a, b)
        if child:
            result[key] = child
    return result


def module_sources(health: dict[str, Any]) -> dict[str, Any]:
    module_map = modules(health)
    ingestion = module_map.get("data-ingestion-service", {})
    event_bus = module_map.get("event-bus", {})
    canonical = ingestion.get("canonical_market_data") or {}
    persistence = ingestion.get("canonical_persistence") or {}
    telemetry = canonical.get("root_cause_telemetry") or {}
    persistence_telemetry = persistence.get("root_cause_telemetry") or {}
    return {
        "canonical_market_data": canonical,
        "canonical_health": canonical.get("health") or {},
        "canonical_transport": canonical.get("transport") or {},
        "gateway_drain": telemetry.get("gateway_drain") or {},
        "gateway_drain_stages": telemetry.get("gateway_drain_stages") or {},
        "canonical_persistence": persistence,
        "persistence_queue_wait": persistence_telemetry.get("queue_wait") or {},
        "persistence_batch_write": persistence_telemetry.get("batch_write") or {},
        "persistence_capacity": persistence_telemetry.get("capacity") or {},
        "event_bus": event_bus,
        "redis_xadd": nested_get(event_bus, ("market_data_e2e", "redis_xadd")) or {},
        "event_loop": nested_get(event_bus, ("runtime_contention", "event_loop")) or {},
        "runtime_streams": nested_get(canonical, ("transport", "runtime_streams"))
        or {},
    }


def latency_delta(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    delta = delta_tree(start, end)
    count = delta.get("count", 0)
    total_ms = delta.get("total_ms", 0.0)
    delta["window_avg_ms"] = (
        round(total_ms / count, 3)
        if isinstance(count, (int, float)) and count > 0
        else 0.0
    )
    return delta


def capacity_delta(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    delta = delta_tree(start, end)
    batches = delta.get("batches", 0)
    batch_events = delta.get("batch_events", 0)
    delta["window_avg_batch_size"] = (
        round(batch_events / batches, 3)
        if isinstance(batches, (int, float)) and batches > 0
        else 0.0
    )
    delta["window_queue_depth_start"] = start.get("queue_depth")
    delta["window_queue_depth_end"] = end.get("queue_depth")
    delta["window_max_queue_depth"] = end.get("max_queue_depth", 0)
    if isinstance(start.get("max_queue_depth"), (int, float)) and isinstance(
        end.get("max_queue_depth"), (int, float)
    ):
        delta["window_max_queue_depth"] = max(
            0, end["max_queue_depth"] - start["max_queue_depth"]
        )
    return delta


def derive_window(
    start_health: dict[str, Any], end_health: dict[str, Any]
) -> dict[str, Any]:
    start = module_sources(start_health)
    end = module_sources(end_health)
    stage_start = start["gateway_drain_stages"]
    stage_end = end["gateway_drain_stages"]
    return {
        "canonical": delta_tree(start["canonical_health"], end["canonical_health"]),
        "gateway_drain": latency_delta(start["gateway_drain"], end["gateway_drain"]),
        "gateway_drain_stages": {
            stage: latency_delta(
                stage_start.get(stage) or {}, stage_end.get(stage) or {}
            )
            for stage in sorted(set(stage_start) | set(stage_end))
        },
        "redis_xadd": latency_delta(start["redis_xadd"], end["redis_xadd"]),
        "event_loop": latency_delta(start["event_loop"], end["event_loop"]),
        "persistence_queue_wait": {
            key: latency_delta(
                start["persistence_queue_wait"].get(key) or {},
                end["persistence_queue_wait"].get(key) or {},
            )
            for key in sorted(
                set(start["persistence_queue_wait"])
                | set(end["persistence_queue_wait"])
            )
        },
        "persistence_batch_write": latency_delta(
            start["persistence_batch_write"], end["persistence_batch_write"]
        ),
        "persistence_capacity": capacity_delta(
            start["persistence_capacity"], end["persistence_capacity"]
        ),
        "runtime_streams": delta_tree(start["runtime_streams"], end["runtime_streams"]),
    }


def main() -> int:
    directory = Path(os.environ.get("AITOS_FORENSIC_REPORT_DIR", "."))
    samples = load_samples(directory / "samples.jsonl")
    if len(samples) < 2:
        raise SystemExit("need at least two valid health samples for window deltas")
    total_delta = derive_window(samples[0]["health"], samples[-1]["health"])
    intervals: list[dict[str, Any]] = []
    for previous, current in zip(samples, samples[1:]):
        interval = derive_window(previous["health"], current["health"])
        interval["window_start"] = previous.get("ts")
        interval["window_end"] = current.get("ts")
        intervals.append(interval)
    output = {
        "sample_count": len(samples),
        "window_start": samples[0].get("ts"),
        "window_end": samples[-1].get("ts"),
        "counter_semantics": "end_minus_start for process-lifetime cumulative counters; latency count/total_ms are differenced to derive window averages; intervals are adjacent health-sample deltas",
        "total_delta": total_delta,
        "intervals": intervals,
    }
    report_json = directory / "report.json"
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["window_deltas"] = output
    report_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_md = directory / "report.md"
    with report_md.open("a", encoding="utf-8") as handle:
        handle.write("\n## Windowed counter attribution v4\n\n")
        handle.write(
            "Persistence telemetry includes queue admission/rejection, queue depth, batch throughput/size, and batch write latency. All cumulative counters are attributed by end-minus-start deltas.\n\n"
        )
        handle.write("```json\n")
        handle.write(json.dumps(output, indent=2, sort_keys=True))
        handle.write("\n```\n")
    (directory / "window_deltas.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
