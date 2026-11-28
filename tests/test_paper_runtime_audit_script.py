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

    assert "set -euo pipefail" in text
    assert "set -x" not in text.lower()
    # Credentials may be read and passed to Redis, but must never be echoed
    # or printed into the audit artifact/logs.
    for line in text.splitlines():
        command = line.lstrip()
        if command.startswith(("echo ", "printf ")):
            assert "$REDIS_PASSWORD" not in command
            assert "$REDISCLI_AUTH" not in command


def test_runtime_audit_script_does_not_abort_on_redis_diagnostic_scan_failure():
    text = SCRIPT.read_text(encoding="utf-8")
    assert (
        'done < <(docker exec -e REDISCLI_AUTH="$REDISCLI_AUTH" aitos-redis redis-cli --raw --scan'
        in text
    )
    assert "2>/dev/null || true)" in text
    assert "Redis maxmemory: unlimited" in text
    assert "=== Audit result ===" in text


def test_production_audit_captures_observability_even_after_canonical_failure():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "paper signal diagnostics" in text
    assert "scanner score breakdown" in text
    assert "scanner ranking decision" in text
    assert "continue-on-error: true" in text
