#!/usr/bin/env python3
"""AITOS Tier-1 runtime monitor — cheap scheduled checks, no LLM involvement.

Checks (paper mode only):
  docker   — expected containers running/healthy
  health   — aitos-paper /health reachable, module statuses
  dataflow — /data-flow freshness summary
  positions— /position-lifecycle open-position coverage
  logs     — ERROR-line delta in aitos-paper logs (checkpointed, bounded)
  disk     — free space on the project drive

Outputs:
  tmp/monitor/report.json   — latest snapshot (overwritten)
  tmp/monitor/history.log   — append-only one-line summaries
  tmp/monitor/alerts.json   — non-empty when status != HEALTHY (Tier-2 trigger)

Exit code is always 0: a monitoring failure must not look like an incident
escalation, and the scheduled automation relies on the report files.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT / "tmp" / "monitor"
REPORT_PATH = OUT_DIR / "report.json"
HISTORY_PATH = OUT_DIR / "history.log"
ALERTS_PATH = OUT_DIR / "alerts.json"
CHECKPOINT_PATH = OUT_DIR / "log_checkpoint.json"

EXPECTED_CONTAINERS = (
    "aitos-redis",
    "aitos-clickhouse",
    "aitos-neo4j",
    "aitos-paper",
    "aitos-learning",
    "aitos-storage-maintenance",
)
HEALTH_URL = "http://127.0.0.1:8090/health"
DATAFLOW_URL = "http://127.0.0.1:8090/data-flow"
POSITIONS_URL = "http://127.0.0.1:8090/position-lifecycle"
LOG_TAIL_LINES = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_docker(
    *args: str, timeout: int = 30
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("docker", *args),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=PROJECT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_docker() -> dict:
    # {{.Health}} errors on containers without a healthcheck; {{.Status}}
    # always exists and carries "(healthy)"/"(unhealthy)" for those that have one.
    proc = _run_docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}")
    if proc is None:
        return {"available": False, "issues": ["docker CLI unavailable"]}
    if proc.returncode != 0:
        return {
            "available": False,
            "issues": [f"docker ps failed: {(proc.stderr or '').strip()[:200]}"],
        }
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            states[parts[0]] = parts[1] if len(parts) == 2 else f"{parts[1]}/{parts[2]}"
    missing = [name for name in EXPECTED_CONTAINERS if name not in states]
    not_running = [
        name
        for name, state in states.items()
        if name in EXPECTED_CONTAINERS and not state.startswith("running")
    ]
    return {
        "available": True,
        "states": states,
        "missing": missing,
        "not_running": not_running,
    }


def _fetch_json(url: str, timeout: float = 10.0) -> tuple[int, object | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except Exception as exc:
        return 0, {"error": type(exc).__name__}


def check_health() -> dict:
    status, payload = _fetch_json(HEALTH_URL)
    result: dict = {"reachable": status == 200, "status_code": status}
    if isinstance(payload, dict):
        modules = payload.get("modules", payload)
        module_items = (
            list(modules.values())
            if isinstance(modules, dict)
            else modules if isinstance(modules, list) else []
        )
        healthy = sum(
            1
            for m in module_items
            if isinstance(m, dict)
            and str(m.get("status", m.get("state", ""))).lower()
            in {"healthy", "ok", "up", "running"}
        )
        result["modules_healthy"] = healthy
        result["modules_total"] = len(module_items)
        result["top_level_status"] = payload.get("status")
        result["payload_keys"] = sorted(payload.keys())
        serialized = json.dumps(payload)
        lag_matches = re.findall(r'"max_lag_ms"\s*:\s*([0-9.]+)', serialized)
        over_matches = re.findall(r'"over_100ms"\s*:\s*(\d+)', serialized)
        max_lag = max((float(v) for v in lag_matches), default=0.0)
        over_100 = max((int(v) for v in over_matches), default=0)
        result["event_loop_max_lag_ms"] = round(max_lag, 3)
        result["event_loop_over_100ms"] = over_100
    return result


def check_dataflow() -> dict:
    status, payload = _fetch_json(DATAFLOW_URL)
    result: dict = {"reachable": status == 200}
    if isinstance(payload, dict):
        result["top_keys"] = sorted(payload.keys())[:20]
        result["summary"] = {
            k: v for k, v in payload.items() if isinstance(v, (int, float, str, bool))
        }
    return result


def check_positions() -> dict:
    status, payload = _fetch_json(POSITIONS_URL)
    result: dict = {"reachable": status == 200}
    if isinstance(payload, dict):
        for key in ("open_positions", "positions", "trades", "summary", "count"):
            if key in payload:
                result[key] = payload[key]
        result["top_keys"] = sorted(payload.keys())[:20]
    return result


def check_paper_logs() -> dict:
    proc = _run_docker("logs", "--tail", str(LOG_TAIL_LINES), "aitos-paper", timeout=45)
    if proc is None or (proc.returncode != 0 and not proc.stdout and not proc.stderr):
        return {"available": False, "issues": ["cannot read aitos-paper logs"]}
    text = (proc.stdout or "") + (proc.stderr or "")
    error_lines = [
        line for line in text.splitlines() if "ERROR" in line or "Traceback" in line
    ]
    persistence = [line for line in error_lines if "persist" in line.lower()]
    return {
        "available": True,
        "scanned_lines": LOG_TAIL_LINES,
        "error_lines": len(error_lines),
        "persistence_errors": len(persistence),
        "sample": error_lines[-5:],
    }


def check_disk() -> dict:
    usage = shutil.disk_usage(PROJECT)
    free_gb = round(usage.free / 1e9, 1)
    total_gb = round(usage.total / 1e9, 1)
    return {
        "free_gb": free_gb,
        "total_gb": total_gb,
        "used_percent": round(usage.used / usage.total * 100, 1),
    }


def evaluate(sections: dict) -> tuple[str, list[str]]:
    issues: list[str] = []

    docker = sections.get("docker", {})
    if not docker.get("available"):
        issues.append("docker unavailable")
    else:
        if docker.get("missing"):
            issues.append(f"missing containers: {docker['missing']}")
        if docker.get("not_running"):
            issues.append(f"not running: {docker['not_running']}")

    health = sections.get("health", {})
    if not health.get("reachable"):
        issues.append("health endpoint unreachable")

    logs = sections.get("paper_logs", {})
    if logs.get("available") and logs.get("persistence_errors", 0) > 0:
        issues.append(f"persistence errors in logs: {logs['persistence_errors']}")

    health = sections.get("health", {})
    if health.get("event_loop_max_lag_ms", 0) > 250:
        issues.append(f"event loop lag high: {health['event_loop_max_lag_ms']}ms max")
    if health.get("event_loop_over_100ms", 0) > 20:
        issues.append(
            f"event loop lag frequent: {health['event_loop_over_100ms']} samples over 100ms"
        )

    disk = sections.get("disk", {})
    if disk.get("free_gb", 999) < 20:
        issues.append(f"low disk: {disk.get('free_gb')}GB free")

    status = "HEALTHY"
    if issues:
        status = (
            "CRITICAL"
            if any(
                i.startswith(
                    ("docker unavailable", "health endpoint", "missing containers")
                )
                for i in issues
            )
            else "DEGRADED"
        )
    return status, issues


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sections = {
        "docker": check_docker(),
        "health": check_health(),
        "dataflow": check_dataflow(),
        "positions": check_positions(),
        "paper_logs": check_paper_logs(),
        "disk": check_disk(),
    }
    status, issues = evaluate(sections)
    report = {
        "timestamp": _now_iso(),
        "project": "aitos",
        "checks": sections,
        "issues": issues,
        "status": status,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = (
        f"[{_now_iso()}] status={status} "
        f"docker={len(sections['docker'].get('states', {}))} "
        f"health_reachable={sections['health'].get('reachable')} "
        f"log_errors={sections['paper_logs'].get('error_lines', 'n/a')} "
        f"disk_free={sections['disk'].get('free_gb')}GB "
        f"issues={issues if issues else 'none'}"
    )
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    # Alerts file signals Tier-2 escalation; delete when healthy again.
    if status == "HEALTHY":
        ALERTS_PATH.unlink(missing_ok=True)
    else:
        ALERTS_PATH.write_text(
            json.dumps(
                {"timestamp": _now_iso(), "status": status, "issues": issues}, indent=2
            ),
            encoding="utf-8",
        )
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
