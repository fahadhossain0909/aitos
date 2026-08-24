from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_runtime_audit.sh"


def test_runtime_audit_script_checks_health_metrics_and_logs():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "/health" in text
    assert "/metrics" in text
    assert "docker stats" in text
    assert "docker system df" in text
    assert "LogPath" in text
    assert "MAX_LOG_MB" in text
    assert "RestartCount" in text


def test_runtime_audit_script_is_fail_safe_and_does_not_expose_secrets():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert "PASSWORD" not in text.upper()
    assert "API_KEY" not in text.upper()
    assert "API_SECRET" not in text.upper()
