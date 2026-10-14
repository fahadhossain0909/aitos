from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_example_uses_local_safe_defaults() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ENVIRONMENT=dev" in env
    assert "AITOS_DATA_ROOT=./data" in env
    assert "MODEL_DATA_DIR=./data/runtime/models" in env
    assert "STORAGE_MAINTENANCE_DRY_RUN=true" in env
    assert "/home/fahad/aitos" not in env
    assert "/mnt/aitos-data" not in env


def test_local_compose_does_not_prune_data_by_default() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    override = (ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert (
        'STORAGE_MAINTENANCE_DRY_RUN: "${STORAGE_MAINTENANCE_DRY_RUN:-true}"'
        in compose
    )
    assert (
        'STORAGE_MAINTENANCE_DRY_RUN: "${STORAGE_MAINTENANCE_DRY_RUN:-true}"'
        in override
    )
    assert "${AITOS_DATA_ROOT:-./data}" in compose
