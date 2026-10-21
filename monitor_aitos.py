#!/usr/bin/env python3
"""
AITOS Project Monitor

Checks the paper-trading stack for operational problems and writes a report.

Portability notes (this runs under Windows Python, where ``shell=True`` uses
cmd.exe):
  * Always invoke commands as argv lists (``shell=False``). cmd.exe does not
    treat single quotes as quoting, so ``docker exec ... sh -c '...'`` silently
    breaks.
  * ``docker compose ps --format json`` emits newline-delimited JSON (one object
    per line), not a JSON array.
  * Do not rely on ``curl`` (the bundled copy is blocked) or on Unix tools such
    as ``tail``/``df`` being on PATH.
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(r"C:\aitos")
REPORT_FILE = PROJECT_DIR / "monitoring_report.json"
LOG_FILE = PROJECT_DIR / "monitoring.log"

HEALTH_URL = "http://localhost:8090/health"
CLICKHOUSE_CONTAINER = "aitos-clickhouse"
CLICKHOUSE_LOG = "/var/log/clickhouse-server/clickhouse-server.err.log"
COMPOSE_PROFILES = ["--profile", "observability"]
PAPER_CONTAINER = "aitos-paper"
PAPER_LOG_WINDOW = "15m"

# A handful of tmp_insert_* dirs is normal: ClickHouse keeps one while an async
# insert is in flight and renames it into place on commit. Only an ACCUMULATION
# means the rename is failing (the corrupted case reached ~27k).
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


def log(message):
    """Write a log entry with timestamp."""
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = f"[{timestamp}] {message}"
    print(entry)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def _find_docker() -> str:
    """Return the docker CLI path.

    The cron scheduler does not inherit the interactive shell PATH, so a bare
    "docker" lookup fails with WinError 2 even though Docker Desktop is
    installed. Fall back to the known install locations.
    """
    found = shutil.which("docker")
    if found:
        return "docker"
    candidates = [
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            r"Programs\DockerDesktop\resources\bin\docker.exe",
        ),
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return "docker"


DOCKER = _find_docker()


def run_argv(argv, timeout=60, cwd=None):
    """Run a command as an argv list (no shell). Returns (returncode, output)."""
    argv = [DOCKER if a == "docker" else a for a in argv]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd or PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except FileNotFoundError as exc:
        return -2, f"command not found: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return -3, str(exc)


def _last_int(text):
    """Return the last integer appearing in text, or -1."""
    if not text:
        return -1
    found = None
    digits = ""
    for ch in text:
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


def check_git_status():
    """Check git status for uncommitted changes (informational only)."""
    rc, output = run_argv(["git", "status", "--porcelain"], timeout=60)
    if rc == 0:
        changes = [line for line in output.splitlines() if line.strip()]
        return {"clean": len(changes) == 0, "changes": changes[:10]}
    return {"clean": False, "error": output.strip()[:200]}


def check_recent_commits():
    """Get recent commit history."""
    rc, output = run_argv(["git", "log", "--oneline", "-5"], timeout=60)
    if rc == 0:
        return {"commits": [line for line in output.splitlines() if line.strip()]}
    return {"error": output.strip()[:200]}


def check_docker_services():
    """Check Docker Compose service status across all profiles."""
    rc, output = run_argv(
        ["docker", "compose", *COMPOSE_PROFILES, "ps", "--format", "json"],
        timeout=90,
    )
    if rc != 0:
        return {"available": False, "error": output.strip()[:200]}

    services = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            services.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    running = [s for s in services if (s.get("State") or "").lower() == "running"]
    unhealthy = [s for s in services if (s.get("Health") or "").lower() == "unhealthy"]
    names = {s.get("Name") for s in services}
    missing = [n for n in EXPECTED_SERVICES if n not in names]

    return {
        "available": True,
        "total": len(services),
        "running": len(running),
        "unhealthy": len(unhealthy),
        "missing": missing,
        "services": [
            {
                "name": s.get("Name"),
                "state": s.get("State"),
                "health": s.get("Health") or "N/A",
            }
            for s in services
        ],
    }


def check_health_endpoint():
    """Check if the paper-trading health endpoint answers 200."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=15) as resp:
            return {"reachable": resp.status == 200, "status_code": resp.status}
    except urllib.error.HTTPError as exc:
        return {"reachable": False, "status_code": exc.code, "error": str(exc)[:120]}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:120]}


def check_clickhouse_write_health():
    """Verify ClickHouse can actually commit MergeTree writes.

    On a Windows 9p/DrvFs bind mount ClickHouse cannot rename temp parts into
    place ("rename: Permission denied"). Inserts then fail with code 1001 and
    orphaned ``tmp_insert_*`` dirs pile up, which later blocks startup for ~30
    minutes while they are cleaned one by one. Data must live on a Docker named
    volume (ext4) for part renames to work.
    """
    # grep exits 1 when nothing matches, which is the healthy case here.
    rc1, out1 = run_argv(
        [
            "docker",
            "exec",
            CLICKHOUSE_CONTAINER,
            "grep",
            "-ac",
            "in rename: Permission denied",
            CLICKHOUSE_LOG,
        ],
        timeout=60,
    )
    rename_errors = _last_int(out1) if rc1 in (0, 1) else -1

    rc2, out2 = run_argv(
        [
            "docker",
            "exec",
            CLICKHOUSE_CONTAINER,
            "find",
            "/var/lib/clickhouse/store",
            "-maxdepth",
            "3",
            "-name",
            "tmp_insert*",
            "-type",
            "d",
        ],
        timeout=180,
    )
    orphans = len([ln for ln in out2.splitlines() if ln.strip()]) if rc2 == 0 else -1

    rc3, out3 = run_argv(
        ["docker", "exec", CLICKHOUSE_CONTAINER, "cat", "/proc/mounts"],
        timeout=60,
    )
    filesystem = "unknown"
    if rc3 == 0:
        for line in out3.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "/var/lib/clickhouse":
                filesystem = parts[2]
                break

    return {
        "rename_errors": rename_errors,
        "orphaned_tmp_dirs": orphans,
        "data_filesystem": filesystem,
        "healthy": (
            rename_errors == 0
            and 0 <= orphans <= ORPHAN_TMP_THRESHOLD
            and filesystem == "ext4"
        ),
    }


def check_paper_trading_errors():
    """Count ERROR lines emitted by the paper-trading container recently."""
    rc, output = run_argv(
        ["docker", "logs", "--since", PAPER_LOG_WINDOW, PAPER_CONTAINER],
        timeout=120,
    )
    if rc != 0:
        return {"available": False, "error": output.strip()[:200]}

    error_lines = [ln for ln in output.splitlines() if '"level": "ERROR"' in ln]
    persistence = [
        ln
        for ln in error_lines
        if "failed to save" in ln
        or "persistence failed" in ln
        or "in rename: Permission denied" in ln
    ]

    # Group by logger + message so the digest can report what is actually
    # happening rather than a bare count. Transient websocket reconnects are
    # expected churn; persistence errors are the ones that mean data loss.
    breakdown = {}
    for line in error_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = f"{rec.get('logger', 'unknown')}: {(rec.get('message') or '')[:60]}"
        breakdown[key] = breakdown.get(key, 0) + 1

    return {
        "available": True,
        "error_lines": len(error_lines),
        "persistence_errors": len(persistence),
        "error_breakdown": dict(
            sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:8]
        ),
    }


def check_test_status():
    """Verify the test suite still collects (uses the running interpreter).

    Returns collectable=None when pytest is simply not installed in the
    interpreter running the monitor (the cron Python may differ from a
    developer one); that is an environment fact, not a project failure.
    """
    rc, output = run_argv(
        [sys.executable, "-m", "pytest", "--co", "-q"],
        timeout=240,
    )
    tail = "\n".join(output.splitlines()[-3:]).strip()
    if rc == 0:
        return {"collectable": True, "output": tail[:300]}
    if "No module named pytest" in output or "No module named 'pytest'" in output:
        return {
            "collectable": None,
            "reason": "pytest unavailable in this interpreter",
            "output": tail[:200],
        }
    return {"collectable": False, "output": tail[:300]}


METRICS_URL = "http://localhost:8090/metrics"

TRADE_TABLES = [
    "trades",
    "trade_runtime_state",
    "journal_entries",
    "decision_journal",
    "learning_experiences",
    "order_book_snapshots",
    "live_analytics_events",
]


def check_trading_summary():
    """Portfolio and recent-trade snapshot.

    Authoritative trade history lives in ClickHouse; the health endpoint adds the
    live open/closed counters that only exist in process memory.
    """
    recent = []
    rc, out = run_argv(
        [
            "docker",
            "exec",
            CLICKHOUSE_CONTAINER,
            "clickhouse-client",
            "--query",
            "SELECT symbol, side, entry_price, state, pnl, entry_time "
            "FROM aitos.trades ORDER BY recorded_at DESC LIMIT 6",
        ],
        timeout=90,
    )
    if rc == 0:
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                pnl = parts[4] if len(parts) > 4 else None
                if pnl in ("\\N", ""):
                    pnl = None
                recent.append(
                    {
                        "symbol": parts[0],
                        "side": parts[1],
                        "entry_price": parts[2],
                        "state": parts[3],
                        "pnl": pnl,
                        "entry_time": parts[5] if len(parts) > 5 else None,
                    }
                )

    counts = {}
    rc2, out2 = run_argv(
        [
            "docker",
            "exec",
            CLICKHOUSE_CONTAINER,
            "clickhouse-client",
            "--query",
            "SELECT table, sum(rows) FROM system.parts WHERE active AND database='aitos' "
            "GROUP BY table ORDER BY sum(rows) DESC",
        ],
        timeout=90,
    )
    if rc2 == 0:
        for line in out2.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    counts[parts[0]] = int(parts[1])
                except ValueError:
                    continue

    live = {}
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=15) as resp:
            health = json.loads(resp.read().decode())
        for module in health.get("modules", []):
            if module.get("module_id") == "trade-lifecycle":
                live = module.get("details") or {}
                break
    except Exception as exc:  # pragma: no cover - defensive
        live = {"error": str(exc)[:120]}

    return {
        "live_open_trades": live.get("open_trades"),
        "live_closed_trades": live.get("closed_trades"),
        "recent_trades": recent,
        "table_rows": {t: counts.get(t, 0) for t in TRADE_TABLES},
    }


METRIC_WANTED = [
    "aitos_event_bus_published_events",
    "aitos_event_bus_consumed_events",
    "aitos_event_bus_handler_failures",
    "aitos_event_bus_retry_events",
    "aitos_event_bus_dlq_events",
    "aitos_trade_lifecycle_open_trades",
    "aitos_trade_lifecycle_closed_trades",
    "aitos_opportunity_scanner_symbols_tracked",
    "aitos_learning_experience_recorder_paper_records_written",
    "aitos_market_os_persistence_events_persisted",
    "aitos_ai_kernel_fusion_min_confidence",
]

TELEMETRY_MODULES = (
    "risk-engine",
    "rl-feedback-loop",
    "ml-explainer-feedback-loop",
    "data-ingestion-service",
    "opportunity-scanner",
    "journal-system",
    "learning-experience-recorder-paper",
    "trade-lifecycle",
)


def check_telemetry_summary():
    """Prometheus metrics plus per-module health detail.

    /metrics is the cheapest source (one HTTP fetch, no ClickHouse round trips)
    and is where the deployed image exposes the platform's own counters.
    """
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=20) as resp:
            text = resp.read().decode()
    except Exception as exc:
        return {"error": str(exc)[:150]}

    metrics = {}
    healthy = 0
    unhealthy = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0].split("{")[0]
        value = parts[-1]
        if key == "aitos_module_healthy":
            if value == "1":
                healthy += 1
            else:
                unhealthy += 1
        elif key in METRIC_WANTED:
            metrics[key] = value

    details = {}
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=20) as resp:
            health = json.loads(resp.read().decode())
        for module in health.get("modules", []):
            mid = module.get("module_id")
            if mid in TELEMETRY_MODULES:
                details[mid] = module.get("details") or {}
    except Exception as exc:  # pragma: no cover - defensive
        details["error"] = str(exc)[:120]

    return {
        "modules_healthy": healthy,
        "modules_unhealthy": unhealthy,
        "metrics": metrics,
        "module_details": details,
    }


def check_disk_space():
    """Check free disk space on the project drive."""
    try:
        usage = shutil.disk_usage(str(PROJECT_DIR.drive) + "\\")
        return {
            "free_gb": round(usage.free / (1024**3), 1),
            "total_gb": round(usage.total / (1024**3), 1),
            "used_percent": round(100 * (1 - usage.free / usage.total), 1),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc)[:150]}


def generate_report():
    """Generate a comprehensive monitoring report."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": "AITOS",
        "checks": {},
    }

    log("Starting monitoring check...")

    report["checks"]["git"] = check_git_status()
    log(f"Git: {'clean' if report['checks']['git']['clean'] else 'has changes'}")

    report["checks"]["commits"] = check_recent_commits()

    docker_status = check_docker_services()
    report["checks"]["docker"] = docker_status
    if docker_status.get("available"):
        log(
            f"Docker: {docker_status['running']}/{docker_status['total']} running, "
            f"{docker_status['unhealthy']} unhealthy, "
            f"missing={docker_status['missing'] or 'none'}"
        )
    else:
        log(f"Docker: not available - {docker_status.get('error', 'unknown')}")

    health = check_health_endpoint()
    report["checks"]["health"] = health
    log(f"Health: {'OK' if health['reachable'] else 'unreachable'}")

    clickhouse = check_clickhouse_write_health()
    report["checks"]["clickhouse"] = clickhouse
    log(
        f"ClickHouse: rename_errors={clickhouse['rename_errors']} "
        f"orphaned_tmp_dirs={clickhouse['orphaned_tmp_dirs']} "
        f"fs={clickhouse['data_filesystem']}"
    )

    paper = check_paper_trading_errors()
    report["checks"]["paper_trading"] = paper
    if paper.get("available"):
        log(
            f"Paper trading: {paper['error_lines']} ERROR lines in last "
            f"{PAPER_LOG_WINDOW} ({paper['persistence_errors']} persistence)"
        )
    else:
        log(f"Paper trading: unavailable - {paper.get('error', 'unknown')}")

    trading = check_trading_summary()
    report["checks"]["trading"] = trading
    log(
        f"Trading: open={trading['live_open_trades']} closed={trading['live_closed_trades']} "
        f"recent={len(trading['recent_trades'])} "
        f"trades_rows={trading['table_rows'].get('trades', 0)}"
    )

    telemetry = check_telemetry_summary()
    report["checks"]["telemetry"] = telemetry
    if "error" in telemetry:
        log(f"Telemetry: unavailable - {telemetry['error']}")
    else:
        m = telemetry["metrics"]
        log(
            f"Telemetry: modules {telemetry['modules_healthy']} healthy / "
            f"{telemetry['modules_unhealthy']} unhealthy, "
            f"published={m.get('aitos_event_bus_published_events')} "
            f"handler_failures={m.get('aitos_event_bus_handler_failures')} "
            f"dlq={m.get('aitos_event_bus_dlq_events')}"
        )

    tests = check_test_status()
    report["checks"]["tests"] = tests
    if tests["collectable"] is True:
        log("Tests: collectable")
    elif tests["collectable"] is False:
        log("Tests: FAILED")
    else:
        log("Tests: pytest unavailable in this interpreter")

    report["checks"]["disk"] = check_disk_space()

    # Determine overall status
    issues = []
    if not docker_status.get("available"):
        issues.append("Docker daemon not reachable")
    else:
        if docker_status["unhealthy"] > 0:
            issues.append(f"{docker_status['unhealthy']} unhealthy Docker service(s)")
        if docker_status["missing"]:
            issues.append(
                f"Containers not running: {', '.join(docker_status['missing'])}"
            )
    if not health["reachable"]:
        issues.append("Health endpoint not reachable on port 8090")
    if (
        clickhouse["rename_errors"] > 0
        or clickhouse["orphaned_tmp_dirs"] > ORPHAN_TMP_THRESHOLD
    ):
        issues.append(
            "ClickHouse writes failing "
            f"(rename_errors={clickhouse['rename_errors']}, "
            f"orphaned_tmp_dirs={clickhouse['orphaned_tmp_dirs']}, "
            f"fs={clickhouse['data_filesystem']}). ClickHouse data must live on an "
            "ext4 Docker named volume, not a 9p/DrvFs Windows bind mount."
        )
    elif clickhouse["data_filesystem"] not in ("ext4", "unknown"):
        issues.append(
            f"ClickHouse data filesystem is '{clickhouse['data_filesystem']}' "
            "(expected ext4 named volume)"
        )
    if paper.get("available") and paper["persistence_errors"] > 0:
        issues.append(
            f"{paper['persistence_errors']} trade-state persistence error(s) in the "
            "last 15 minutes"
        )
    if "error" not in telemetry:
        m = telemetry["metrics"]

        def _num(key):
            try:
                return float(m.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        if telemetry["modules_unhealthy"] > 0:
            issues.append(
                f"{telemetry['modules_unhealthy']} internal module(s) reporting unhealthy"
            )
        if _num("aitos_event_bus_handler_failures") > 5:
            issues.append(
                f"{int(_num('aitos_event_bus_handler_failures'))} event-bus handler failure(s) — a single recovered failure is normal churn"
            )
        if _num("aitos_event_bus_dlq_events") > 0:
            issues.append(
                f"{int(_num('aitos_event_bus_dlq_events'))} event(s) in the dead-letter queue"
            )
    else:
        issues.append(f"Telemetry endpoint unreachable: {telemetry['error']}")
    if tests["collectable"] is False:
        issues.append("Test collection failed")

    report["issues"] = issues
    report["status"] = "HEALTHY" if not issues else "NEEDS_ATTENTION"

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    log(f"Report saved. Status: {report['status']}")
    for issue in issues:
        log(f"  ISSUE: {issue}")

    return report


if __name__ == "__main__":
    report = generate_report()
    print(f"\n{'=' * 60}")
    print(f"AITOS Monitoring Report - {report['timestamp']}")
    print(f"Status: {report['status']}")
    print(f"{'=' * 60}")
    if report["issues"]:
        print("\nIssues found:")
        for issue in report["issues"]:
            print(f"  - {issue}")
    else:
        print("\nNo issues detected.")
    print(f"\nFull report: {REPORT_FILE}")
