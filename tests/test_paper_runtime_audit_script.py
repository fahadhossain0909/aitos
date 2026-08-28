from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_runtime_audit.sh"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "production-audit.yml"


def test_runtime_audit_script_checks_health_metrics_and_logs():
    text = SCRIPT.read_text(encoding="utf-8")
    checks = (
        "/health",
        "/metrics",
        "docker stats",
        "docker system df",
        "LogPath",
        "MAX_LOG_MB",
        "RestartCount",
        "Paper signal diagnostics",
    )

    for marker in checks:
        assert marker in text


def test_runtime_audit_script_is_fail_safe_and_does_not_expose_secrets():
    text = SCRIPT.read_text(encoding="utf-8")
    forbidden = ("PASSWORD", "API_KEY", "API_SECRET")

    assert "set -euo pipefail" in text
    for marker in forbidden:
        assert marker not in text.upper()


def test_runtime_audit_script_does_not_abort_on_redis_diagnostic_scan_failure():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "done < <(docker exec aitos-redis redis-cli --scan" in text
    assert "Redis maxmemory: unlimited" in text
    assert "=== Audit result ===" in text


def test_production_audit_captures_observability_even_after_canonical_failure():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "paper signal diagnostics" in text
    assert "scanner score breakdown" in text
    assert "scanner ranking decision" in text
    assert "continue-on-error: true" in text
