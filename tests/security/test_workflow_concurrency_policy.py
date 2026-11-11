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
