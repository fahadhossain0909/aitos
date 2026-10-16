#!/usr/bin/env python3
"""
AITOS deterministic status probe (for the cron change-detector).

Prints a stable, timestamp-free snapshot of the paper-trading stack. The cron
``monitor`` gate compares this output tick to tick: identical output skips the
agent entirely, changed output wakes it with a diff. Anything non-deterministic
here (timestamps, durations, random ids) would make every tick look changed, so
keep this output purely state-based.
"""

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(r"C:\aitos")
HEALTH_URL = "http://localhost:8090/health"
COMPOSE_PROFILES = ["--profile", "observability"]

# See monitor_aitos.py: a few tmp_insert_* dirs are normal while async inserts
# are in flight; only an accumulation indicates failing part renames.
ORPHAN_TMP_THRESHOLD = 100

EXPECTED_SERVICES = [
    "aitos-paper",
    "aitos-learning",
    "aitos-clickhouse",
    "aitos-redis",
    "aitos-neo4j",
    "aitos-redis-dlq-retention",
    "aitos-redis-stream-archive",
    "aitos-prometheus",
    "aitos-grafana",
    "aitos-storage-maintenance",
]


def run_argv(argv, timeout=90):
    try:
        proc = subprocess.run(
            argv,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # pragma: no cover - defensive
        return -1, str(exc)


def _last_int(text):
    found = None
    digits = ""
    for ch in text or "":
        if ch.isdigit():
            digits += ch
        else:
            if digits:
                found = digits
            digits = ""
    if digits:
        found = digits
    try:
        return int(found) if found is not None else -1
    except ValueError:
        return -1


def main():
    lines = []

    rc, out = run_argv(["docker", "compose", *COMPOSE_PROFILES, "ps", "--format", "json"])
    if rc != 0:
        lines.append("docker=unavailable")
    else:
        services = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        names = {s.get("Name") for s in services}
        running = sorted(
            s.get("Name") for s in services if (s.get("State") or "").lower() == "running"
        )
        unhealthy = sorted(
            s.get("Name") for s in services if (s.get("Health") or "").lower() == "unhealthy"
        )
        missing = sorted(n for n in EXPECTED_SERVICES if n not in names)
        lines.append(f"containers_running={len(running)}/{len(EXPECTED_SERVICES)}")
        lines.append(f"unhealthy={','.join(unhealthy) or 'none'}")
        lines.append(f"missing={','.join(missing) or 'none'}")

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=15) as resp:
            lines.append(f"health={resp.status}")
    except urllib.error.HTTPError as exc:
        lines.append(f"health={exc.code}")
    except Exception:
        lines.append("health=unreachable")

    rc, out = run_argv(
        [
            "docker", "exec", "aitos-clickhouse",
            "grep", "-ac", "Code: 1001",
            "/var/log/clickhouse-server/clickhouse-server.err.log",
        ]
    )
    code1001 = _last_int(out) if rc in (0, 1) else -1

    rc, out = run_argv(
        [
            "docker", "exec", "aitos-clickhouse",
            "find", "/var/lib/clickhouse/store",
            "-maxdepth", "3", "-name", "tmp_insert*", "-type", "d",
        ],
        timeout=180,
    )
    orphans = len([ln for ln in out.splitlines() if ln.strip()]) if rc == 0 else -1

    # Emit a coarse verdict, not the raw count. A few tmp_insert_* dirs exist
    # while async inserts are in flight, so the raw number changes every tick and
    # would wake the agent constantly. Only an accumulation means real failure.
    writes_ok = code1001 == 0 and 0 <= orphans <= ORPHAN_TMP_THRESHOLD
    lines.append(f"clickhouse_writes={'ok' if writes_ok else 'failing'}")

    rc, out = run_argv(["docker", "exec", "aitos-clickhouse", "cat", "/proc/mounts"])
    fs = "unknown"
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "/var/lib/clickhouse":
                fs = parts[2]
                break
    lines.append(f"clickhouse_fs={fs}")

    rc, out = run_argv(["docker", "logs", "--since", "15m", "aitos-paper"], timeout=120)
    if rc != 0:
        lines.append("paper=unavailable")
    else:
        errs = [ln for ln in out.splitlines() if '"level": "ERROR"' in ln]
        persist = [
            ln for ln in errs
            if "failed to save" in ln or "persistence failed" in ln or "Code: 1001" in ln
        ]
        # Only a data-loss signal belongs in the change-detector. Raw error counts
        # change every tick (websocket churn) and would wake the agent constantly.
        lines.append(f"paper_persistence_errors={len(persist)}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
