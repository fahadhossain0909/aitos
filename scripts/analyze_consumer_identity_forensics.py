#!/usr/bin/env python3
"""Analyze repeated Redis consumer snapshots without changing runtime behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STREAMS = (
    "market.trade",
    "market.book.delta",
    "market.book.snapshot",
    "market.ticker",
    "market.funding",
    "market.open_interest",
    "market.liquidation",
    "market.options",
    "market.instrument",
)


def load_consumers(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    result: set[str] = set()
    if not isinstance(data, list):
        return result
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result.add(item["name"])
    return result


def runtime(path: Path) -> tuple[int | None, str | None]:
    try:
        parts = path.read_text().strip().split(maxsplit=1)
    except OSError:
        return None, None
    if not parts:
        return None, None
    try:
        restarts = int(parts[0])
    except ValueError:
        restarts = None
    return restarts, parts[1] if len(parts) > 1 else None


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} OUT_DIR")
    root = Path(sys.argv[1])
    snapshots = sorted(p for p in (root / "snapshots").iterdir() if p.is_dir())
    report: list[str] = [
        "# Redis Consumer Identity Forensics",
        "",
        "This report is observational only. It does not delete consumers or change subscription behavior.",
        "",
    ]
    aggregate: dict[str, dict[str, object]] = {}
    churn_events: list[dict[str, object]] = []

    for stream in STREAMS:
        previous: set[str] | None = None
        stream_rows: list[dict[str, object]] = []
        for snap in snapshots:
            names = load_consumers(snap / f"{stream}.json")
            restarts, started_at = runtime(snap / "redis_runtime.txt")
            if previous is not None:
                added = sorted(names - previous)
                removed = sorted(previous - names)
                if added or removed:
                    event = {
                        "snapshot": snap.name,
                        "stream": stream,
                        "added": added,
                        "removed": removed,
                        "redis_restart_count": restarts,
                    }
                    churn_events.append(event)
            else:
                added = sorted(names)
                removed = []
            stream_rows.append(
                {
                    "snapshot": snap.name,
                    "consumer_count": len(names),
                    "redis_restart_count": restarts,
                    "started_at": started_at,
                }
            )
            previous = names

        unique = set()
        for snap in snapshots:
            unique.update(load_consumers(snap / f"{stream}.json"))
        aggregate[stream] = {
            "unique_consumers_seen": len(unique),
            "final_consumer_count": stream_rows[-1]["consumer_count"] if stream_rows else 0,
            "timeline": stream_rows,
        }

    restart_changes = []
    previous_restart: int | None = None
    for snap in snapshots:
        restarts, _ = runtime(snap / "redis_runtime.txt")
        if restarts is not None and previous_restart is not None and restarts != previous_restart:
            restart_changes.append({"snapshot": snap.name, "from": previous_restart, "to": restarts})
        if restarts is not None:
            previous_restart = restarts

    report.append("## Evidence")
    report.append("")
    report.append(f"- Snapshots: **{len(snapshots)}**")
    report.append(f"- Consumer-set churn events: **{len(churn_events)}**")
    report.append(f"- Redis restart-count changes: **{len(restart_changes)}**")
    report.append("")
    report.append("## Interpretation")
    report.append("")
    if not churn_events:
        report.append("- No consumer-name churn was observed during this window; a stable-identity code change is not justified by this run alone.")
    else:
        report.append("- Consumer-name churn was observed. Compare each churn event with the Redis restart count before changing identity semantics.")
        restart_at_churn = [e for e in churn_events if e.get("redis_restart_count") in {x["to"] for x in restart_changes}]
        if restart_changes and restart_at_churn:
            report.append("- At least some churn coincides with Redis restart-count changes; restart-generated stale consumers remain plausible.")
        else:
            report.append("- Churn occurred without a matching Redis restart-count change in the same snapshot; investigate subscription recreation inside the application before changing consumer identity.")
    report.append("")
    report.append("## Per-stream summary")
    report.append("")
    for stream, data in aggregate.items():
        report.append(
            f"- `{stream}`: unique consumers seen={data['unique_consumers_seen']}, "
            f"final count={data['final_consumer_count']}"
        )

    (root / "consumer_identity_report.md").write_text("\n".join(report) + "\n")
    (root / "consumer_identity_events.json").write_text(
        json.dumps(
            {"churn_events": churn_events, "restart_changes": restart_changes, "streams": aggregate},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
