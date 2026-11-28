import subprocess
from pathlib import Path

SCRIPT = Path("scripts/bootstrap_storage.sh").read_text(encoding="utf-8")
ENV = Path(".env.example").read_text(encoding="utf-8")
WORKFLOW = Path(".github/workflows/staging-cd.yml").read_text(encoding="utf-8")


def test_storage_bootstrap_selects_root_or_external_uuid_without_implicit_fallback() -> (
    None
):
    assert 'if [[ -z "$DISK_UUID" ]]; then' in SCRIPT
    assert "ROOT_DISK_MODE=true" in SCRIPT
    assert 'DATA_UUID" == "$ROOT_UUID"' in SCRIPT
    assert "else\n  ROOT_DISK_MODE=false" in SCRIPT
    assert "Configured external data UUID was not found" in SCRIPT
    assert 'MOUNT_UUID" == "$DISK_UUID"' in SCRIPT
    assert "AITOS_ALLOW_BOOT_DISK_DATA" not in SCRIPT


def test_storage_bootstrap_refuses_to_hide_existing_data_when_mounting_external_disk() -> (
    None
):
    assert "Unmounted data root contains existing files" in SCRIPT
    assert "mounted from UUID" in SCRIPT
    assert "Persisting external mount in /etc/fstab" in SCRIPT


def test_storage_bootstrap_supports_optional_sudo_password_without_persisting_it() -> (
    None
):
    assert "sudo -n -v" in SCRIPT
    assert "sudo -S -p ''" in SCRIPT
    assert "printf '%s\\n' \"$AITOS_SUDO_PASSWORD\"" in SCRIPT
    assert "AITOS_SUDO_PASSWORD" not in ENV
    assert (
        "AITOS_SUDO_PASSWORD"
        not in WORKFLOW.split("cat > .env << ENVEOF", 1)[1].split("ENVEOF", 1)[0]
    )


def test_storage_environment_contract_exists() -> None:
    for name in (
        "AITOS_DATA_ROOT",
        "AITOS_DATA_DISK_UUID",
        "AITOS_HOST_UID",
        "AITOS_HOST_GID",
        "CLICKHOUSE_UID",
        "REDIS_UID",
        "NEO4J_UID",
    ):
        assert f"{name}=" in ENV


def test_staging_workflow_accepts_optional_external_uuid_and_sudo_secret() -> None:
    assert "AITOS_DATA_DISK_UUID: ${{ secrets.AITOS_DATA_DISK_UUID }}" in WORKFLOW
    assert "AITOS_SUDO_PASSWORD: ${{ secrets.AITOS_SUDO_PASSWORD }}" in WORKFLOW
    assert "AITOS_DATA_DISK_UUID:=}" in WORKFLOW
    assert "Storage mode: OS root filesystem UUID" in WORKFLOW
    assert "Storage mode: external UUID" in WORKFLOW
    assert '[ "$DATA_UUID" = "$ROOT_UUID" ]' in WORKFLOW
    assert '[ "$DATA_UUID" = "$AITOS_DATA_DISK_UUID" ]' in WORKFLOW
    assert "AITOS_ALLOW_BOOT_DISK_DATA" not in WORKFLOW


def test_staging_enable_switch_requires_manual_confirmation() -> None:
    assert "workflow_dispatch:" in WORKFLOW
    assert "confirm_deploy:" in WORKFLOW
    trigger = WORKFLOW.split("permissions:", 1)[0]
    assert "  push:" not in trigger
    gate = WORKFLOW.split("  staging-gate:", 1)[1].split("  publish-image:", 1)[0]
    assert "environment: staging" in gate
    assert "AITOS_VPS_ENABLED: ${{ vars.AITOS_VPS_ENABLED }}" in gate
    assert "CONFIRM_DEPLOY: ${{ inputs.confirm_deploy }}" in gate
    assert (
        '[[ "${CONFIRM_DEPLOY:-false}" == "true" && '
        '"${AITOS_VPS_ENABLED:-}" == "true" ]]'
    ) in gate
    assert 'echo "enabled=true" >> "$GITHUB_OUTPUT"' in gate
    assert 'echo "enabled=false" >> "$GITHUB_OUTPUT"' in gate


def test_staging_jobs_use_gate_output_not_repository_scope_condition() -> None:
    assert "vars.AITOS_VPS_ENABLED == 'true'" not in WORKFLOW
    publish = WORKFLOW.split("  publish-image:", 1)[1].split("  deploy-and-audit:", 1)[
        0
    ]
    deploy = WORKFLOW.split("  deploy-and-audit:", 1)[1]
    assert "needs: staging-gate" in publish
    assert "needs.staging-gate.outputs.enabled == 'true'" in publish
    assert "needs: [staging-gate, publish-image]" in deploy
    assert "needs.staging-gate.outputs.enabled == 'true'" in deploy


def test_storage_bootstrap_shell_syntax() -> None:
    subprocess.run(["bash", "-n", "scripts/bootstrap_storage.sh"], check=True)
