from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_auto_fix_workflow_is_read_only_and_does_not_shell_interpolate_commit_text():
    source = (ROOT / ".github/workflows/auto-fix.yml").read_text(encoding="utf-8")

    assert "contents: read" in source
    assert "contents: write" not in source
    assert "git commit" not in source
    assert "git push" not in source
    assert "--fix" not in source
    assert "github.event.head_commit.message" not in source


def test_ci_test_environment_uses_disposable_credentials_not_repository_secrets():
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    test_env = source.split("      - name: Create .env file for tests", 1)[1].split(
        "      - name: Run unit and integration tests", 1
    )[0]
    compose_setup = source.split("      - name: Prepare safe CI environment", 1)[1].split(
        "      - name: Validate Docker Compose configuration", 1
    )[0]

    assert "ci-only-not-a-secret" in test_env
    assert "secrets." not in test_env
    assert "DATABASE_URL=sqlite:///./.ci-test.db" in test_env
    assert "AITOS_RUNTIME_IMAGE=ghcr.io/" in compose_setup


def test_compose_ci_validates_normalized_model_not_only_yaml_syntax():
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docker compose config --format json" in source
    assert "scripts/validate_compose_policy.py" in source
    assert "AITOS_REQUIRE_DIGEST_IMAGES=true" in source
