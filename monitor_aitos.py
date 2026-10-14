#!/usr/bin/env python3
"""
AITOS Project Monitor
Monitors the AITOS trading system for issues and reports them.
"""

import subprocess
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(r"C:\aitos")
REPORT_FILE = PROJECT_DIR / "monitoring_report.json"
LOG_FILE = PROJECT_DIR / "monitoring.log"

def log(message):
    """Write a log entry with timestamp."""
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = f"[{timestamp}] {message}"
    print(entry)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def run_command(cmd, cwd=None, timeout=60):
    """Run a shell command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd or str(PROJECT_DIR),
            capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)

def check_git_status():
    """Check git status for uncommitted changes."""
    success, output = run_command("git status --porcelain")
    if success:
        changes = [line for line in output.strip().split("\n") if line.strip()]
        return {"clean": len(changes) == 0, "changes": changes[:10]}
    return {"clean": False, "error": output}

def check_docker_services():
    """Check Docker Compose service status."""
    success, output = run_command("docker compose ps --format json", timeout=30)
    if not success:
        return {"available": False, "error": output.strip()[:200]}
    
    try:
        services = json.loads(output) if output.strip() else []
        running = [s for s in services if s.get("State") == "running"]
        unhealthy = [s for s in services if s.get("Health") == "unhealthy"]
        return {
            "available": True,
            "total": len(services),
            "running": len(running),
            "unhealthy": len(unhealthy),
            "services": [{"name": s.get("Name"), "state": s.get("State"), "health": s.get("Health", "N/A")} for s in services[:10]]
        }
    except json.JSONDecodeError:
        return {"available": True, "raw": output[:500]}

def check_health_endpoint():
    """Check if the health endpoint is reachable."""
    success, output = run_command("curl -s -o /dev/null -w \"%{http_code}\" http://localhost:8090/health", timeout=10)
    if success:
        status_code = output.strip()
        return {"reachable": status_code == "200", "status_code": status_code}
    return {"reachable": False, "error": output.strip()[:100]}

def check_test_status():
    """Run a quick test collection to verify test infrastructure."""
    success, output = run_command("python -m pytest --co -q 2>&1 | tail -5", timeout=60)
    if success:
        return {"collectable": True, "output": output.strip()[:200]}
    return {"collectable": False, "error": output.strip()[:300]}

def check_recent_commits():
    """Get recent commit history."""
    success, output = run_command("git log --oneline -5")
    if success:
        commits = [line for line in output.strip().split("\n") if line.strip()]
        return {"commits": commits}
    return {"error": output}

def check_disk_space():
    """Check disk space on the project drive."""
    success, output = run_command("df -h /c 2>/dev/null || df -h .", timeout=10)
    if success:
        lines = output.strip().split("\n")
        return {"output": lines[-1] if lines else "N/A"}
    return {"error": output}

def generate_report():
    """Generate a comprehensive monitoring report."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": "AITOS",
        "checks": {}
    }
    
    log("Starting monitoring check...")
    
    # Git status
    report["checks"]["git"] = check_git_status()
    log(f"Git: {'clean' if report['checks']['git']['clean'] else 'has changes'}")
    
    # Docker
    report["checks"]["docker"] = check_docker_services()
    docker_status = report["checks"]["docker"]
    if docker_status.get("available"):
        log(f"Docker: {docker_status.get('running', 0)}/{docker_status.get('total', 0)} running")
    else:
        log(f"Docker: not available - {docker_status.get('error', 'unknown')}")
    
    # Health endpoint
    report["checks"]["health"] = check_health_endpoint()
    log(f"Health: {'OK' if report['checks']['health']['reachable'] else 'unreachable'}")
    
    # Test infrastructure
    report["checks"]["tests"] = check_test_status()
    log(f"Tests: {'collectable' if report['checks']['tests']['collectable'] else 'FAILED'}")
    
    # Recent commits
    report["checks"]["commits"] = check_recent_commits()
    
    # Disk space
    report["checks"]["disk"] = check_disk_space()
    
    # Determine overall status
    issues = []
    if not report["checks"]["git"]["clean"]:
        issues.append("Uncommitted changes in working tree")
    if not report["checks"]["health"]["reachable"]:
        issues.append("Health endpoint not reachable")
    if not report["checks"]["tests"]["collectable"]:
        issues.append("Test collection failed")
    if report["checks"]["docker"].get("available") and report["checks"]["docker"].get("unhealthy", 0) > 0:
        issues.append(f"{report['checks']['docker']['unhealthy']} unhealthy Docker services")
    
    report["issues"] = issues
    report["status"] = "HEALTHY" if not issues else "NEEDS_ATTENTION"
    
    # Save report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    log(f"Report saved. Status: {report['status']}")
    if issues:
        for issue in issues:
            log(f"  ISSUE: {issue}")
    
    return report

if __name__ == "__main__":
    report = generate_report()
    print(f"\n{'='*60}")
    print(f"AITOS Monitoring Report - {report['timestamp']}")
    print(f"Status: {report['status']}")
    print(f"{'='*60}")
    if report["issues"]:
        print("\nIssues found:")
        for issue in report["issues"]:
            print(f"  - {issue}")
    else:
        print("\nNo issues detected.")
    print(f"\nFull report: {REPORT_FILE}")
