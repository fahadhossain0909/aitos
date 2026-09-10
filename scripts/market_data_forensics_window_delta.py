#!/usr/bin/env python3
"""Add start/end deltas to an E2E forensic bundle.

Health/transport counters are process-lifetime values. This helper attributes
counter changes to the actual audit window without changing runtime behavior.
"""
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
}


def load_samples(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def first_health(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("health", {})
    return value if isinstance(value, dict) else {}


def nested_get(root: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = root
        ok = True
        for key in path:
            if not isinstance(value, dict) or key not in value:
                ok = False
                break
            value = value[key]
        if ok:
            return value
    return None


def delta_tree(start: Any, end: Any) -> Any:
    if isinstance(start, dict) and isinstance(end, dict):
        result: dict[str, Any] = {}
        for key in sorted(set(start) | set(end)):
            if key in COUNTER_KEYS:
                a, b = start.get(key), end.get(key)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    result[key] = b - a
                continue
            child = delta_tree(start.get(key), end.get(key))
            if child not in ({}, None):
                result[key] = child
        return result
    return None


def main() -> int:
    directory = Path(os.environ.get("AITOS_FORENSIC_REPORT_DIR", "."))
    samples = load_samples(directory / "samples.jsonl")
    if len(samples) < 2:
        raise SystemExit("need at least two health samples for window deltas")

    start = first_health(samples[0])
    end = first_health(samples[-1])

    # Keep the complete attribution tree for canonical/transport telemetry.
    start_sources = {
        "canonical_market_data": nested_get(
            start,
            ("details", "canonical_market_data"),
            ("canonical_market_data",),
        ),
        "transport_telemetry": nested_get(
            start,
            ("details", "transport_telemetry"),
            ("transport_telemetry",),
        ),
        "canonical_persistence": nested_get(
            start,
            ("details", "canonical_persistence"),
            ("canonical_persistence",),
        ),
    }
    end_sources = {
        "canonical_market_data": nested_get(
            end,
            ("details", "canonical_market_data"),
            ("canonical_market_data",),
        ),
        "transport_telemetry": nested_get(
            end,
            ("details", "transport_telemetry"),
            ("transport_telemetry",),
        ),
        "canonical_persistence": nested_get(
            end,
            ("details", "canonical_persistence"),
            ("canonical_persistence",),
        ),
    }
    deltas = {
        name: delta_tree(start_sources[name], end_sources[name])
        for name in start_sources
    }

    # Runtime stream telemetry is cumulative too; explicitly expose the exact
    # per-stream window deltas so an audit cannot mistake lifetime counts for
    # failures occurring during its observation period.
    runtime_start = nested_get(
        start_sources["transport_telemetry"] or {}, ("runtime_streams",)
    ) or {}
    runtime_end = nested_get(
        end_sources["transport_telemetry"] or {}, ("runtime_streams",)
    ) or {}
    runtime_deltas = delta_tree(runtime_start, runtime_end)
    deltas["runtime_streams"] = runtime_deltas or {}

    output = {
        "sample_count": len(samples),
        "window_start": samples[0].get("ts"),
        "window_end": samples[-1].get("ts"),
        "counter_semantics": "end_minus_start; counters are process-lifetime unless explicitly reset by the application",
        "deltas": deltas,
    }

    report_json = directory / "report.json"
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["window_deltas"] = output
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report_md = directory / "report.md"
    with report_md.open("a", encoding="utf-8") as fh:
        fh.write("\n## Windowed counter attribution\n\n")
        fh.write(
            "Counters below are **end minus start** across this audit window; "
            "they must not be interpreted as process-lifetime totals.\n\n"
        )
        fh.write("```json\n")
        fh.write(json.dumps(output, indent=2, sort_keys=True))
        fh.write("\n```\n")

    (directory / "window_deltas.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
