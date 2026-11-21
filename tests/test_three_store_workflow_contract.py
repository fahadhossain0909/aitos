from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "three-store-recovery-integration.yml"
)


def test_three_store_workflow_has_branch_push_and_manual_dispatch_triggers():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert '      - "audit/system-audit-and-docs-2026-11"' in source
    assert "\n  workflow_dispatch:\n" in source


def test_testnet_inventory_preflight_requires_explicit_opt_in():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "github.event_name == 'workflow_dispatch' || "
        "(github.event_name == 'push' && "
        "contains(github.event.head_commit.message, '[testnet-readonly]'))"
    ) in source
    assert (
        "pytest -q tests/integration/test_execution_recovery_three_store.py "
        "-k authenticated_binance_demo_testnet_read_only_preflight"
    ) in source


def test_successful_three_store_push_dispatches_full_repository_ci():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "if: github.event_name == 'push' && "
        "github.ref_name == 'audit/system-audit-and-docs-2026-11'"
    ) in source
    assert "/actions/workflows/ci.yml/dispatches" in source
