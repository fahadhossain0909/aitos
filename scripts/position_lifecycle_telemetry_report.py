#!/usr/bin/env python3
"""Summarize structured position lifecycle events from AITOS JSON logs.

Usage:
  python scripts/position_lifecycle_telemetry_report.py aitos.log
  cat aitos.log | python scripts/position_lifecycle_telemetry_report.py -
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

CLOSE_EVENTS = {
    "position.close.attempt",
    "position.close.completed",
    "position.close.failed",
}
EVALUATION_EVENTS = {
    "position.evaluation.completed",
    "position.monitor.heartbeat",
    "position.monitor.stale_candidate",
    "position.monitor.stale_recovered",
    "position.monitor.stale_recovery_failed",
}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def load_events(lines) -> tuple[dict[str, list[dict[str, Any]]], int]:
    positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(payload, dict):
            continue
        event = payload.get("event")
        position_id = payload.get("position_id") or payload.get("trade_id")
        if event and position_id:
            positions[str(position_id)].append(payload)
    for events in positions.values():
        events.sort(
            key=lambda item: _parse_time(item.get("timestamp"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
    return positions, malformed


def build_report(
    positions,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 120.0,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = []
    for position_id, events in sorted(positions.items()):
        last = events[-1]
        last_time = _parse_time(last.get("timestamp"))
        last_eval = next(
            (e for e in reversed(events) if e.get("event") in EVALUATION_EVENTS),
            None,
        )
        eval_time = _parse_time(last_eval.get("timestamp")) if last_eval else None
        latest_close = next(
            (e for e in reversed(events) if e.get("event") in CLOSE_EVENTS),
            None,
        )
        latest_event_name = str(last.get("event", "unknown"))
        state = str(last.get("state", last.get("confirmed_state", "unknown")))
        age_seconds = max(0.0, (now - eval_time).total_seconds()) if eval_time else None
        close_outcome = str(latest_close.get("event")) if latest_close else "no_close_event"
        rows.append({
            "position_id": position_id,
            "symbol": last.get("symbol", "unknown"),
            "state": state,
            "event_count": len(events),
            "last_event": latest_event_name,
            "last_event_at": last.get("timestamp"),
            "last_evaluation_at": last_eval.get("timestamp") if last_eval else None,
            "evaluation_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "monitor_stale": age_seconds is None or age_seconds > stale_after_seconds,
            "close_evidence": close_outcome,
            "trace_id": last.get("trace_id", f"position:{position_id}"),
        })
    return {
        "generated_at": now.isoformat(),
        "position_count": len(rows),
        "stale_or_unobserved_count": sum(row["monitor_stale"] for row in rows),
        "positions": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logfile", help="JSON-lines AITOS log file, or '-' for stdin")
    parser.add_argument("--stale-after-seconds", type=float, default=120.0)
    args = parser.parse_args()
    stream = (
        sys.stdin
        if args.logfile == "-"
        else open(args.logfile, encoding="utf-8")
    )
    try:
        positions, malformed = load_events(stream)
    finally:
        if stream is not sys.stdin:
            stream.close()
    report = build_report(
        positions, stale_after_seconds=max(1.0, args.stale_after_seconds)
    )
    print(json.dumps({**report, "malformed_lines": malformed}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
