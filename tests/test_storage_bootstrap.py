from pathlib import Path
import subprocess

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
    assert 'printf \'%s\\n\' "$AITOS_SUDO_PASSWORD"' in SCRIPT
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


def test_storage_bootstrap_shell_syntax() -> None:
    subprocess.run(["bash", "-n", "scripts/bootstrap_storage.sh"], check=True)
