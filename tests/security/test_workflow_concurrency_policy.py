"""Protect PR validation runs from being cancelled by rapid branch updates."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATION_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/console-quality.yml",
    ".github/workflows/isolated-redis-recovery.yml",
    ".github/workflows/ledger-quality.yml",
    ".github/workflows/linux-console.yml",
    ".github/workflows/operator-quality.yml",
    ".github/workflows/paper-canary.yml",
    ".github/workflows/persistence-quality.yml",
    ".github/workflows/postgres-storage-integration.yml",
    ".github/workflows/security-intent-quality.yml",
    ".github/workflows/tauri-desktop.yml",
)


@pytest.mark.parametrize("workflow", VALIDATION_WORKFLOWS)
def test_validation_workflows_do_not_cancel_in_progress_runs(workflow):
    content = (ROOT / workflow).read_text(encoding="utf-8")
    assert "cancel-in-progress: false" in content


@pytest.mark.parametrize(
    "workflow",
    (
        ".github/workflows/console-quality.yml",
        ".github/workflows/windows-console.yml",
        ".github/workflows/linux-console.yml",
        ".github/workflows/tauri-desktop.yml",
    ),
)
def test_desktop_validation_repeats_after_matching_mainline_changes(workflow):
    content = (ROOT / workflow).read_text(encoding="utf-8")
    push_config = content.split("  pull_request:", 1)[0]
    assert "  push:" in push_config
    assert "branches: [main" in push_config
    assert "  paths:" in push_config


@pytest.mark.parametrize(
    "workflow",
    (
        ".github/workflows/cd.yml",
        ".github/workflows/staging-cd.yml",
    ),
)
def test_container_publication_emits_sbom_and_provenance_without_enabling_deploy(
    workflow,
):
    content = (ROOT / workflow).read_text(encoding="utf-8")
    assert "provenance: mode=max" in content
    assert "sbom: true" in content
    if workflow.endswith("/cd.yml"):
        # Production deployment remains repository-variable gated.
        assert "if: ${{ vars.AITOS_VPS_ENABLED == 'true' }}" in content
    else:
        # Staging uses an environment-scoped gate, then passes its output
        # to the image-publish and deploy jobs.
        gate = content.split("  staging-gate:", 1)[1].split("  publish-image:", 1)[0]
        publish = content.split("  publish-image:", 1)[1].split(
            "  deploy-and-audit:", 1
        )[0]
        deploy = content.split("  deploy-and-audit:", 1)[1]
        assert "environment: staging" in gate
        assert "AITOS_VPS_ENABLED: ${{ vars.AITOS_VPS_ENABLED }}" in gate
        assert "needs: staging-gate" in publish
        assert "needs.staging-gate.outputs.enabled == 'true'" in publish
        assert "needs: [staging-gate, publish-image]" in deploy
        assert "needs.staging-gate.outputs.enabled == 'true'" in deploy
