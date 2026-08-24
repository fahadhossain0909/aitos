from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_runtime_audit.sh"


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
    )

    for marker in checks:
        assert marker in text


def test_runtime_audit_script_is_fail_safe_and_does_not_expose_secrets():
    text = SCRIPT.read_text(encoding="utf-8")
    forbidden = ("PASSWORD", "API_KEY", "API_SECRET")

    assert "set -euo pipefail" in text
    for marker in forbidden:
        assert marker not in text.upper()
