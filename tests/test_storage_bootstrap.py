from pathlib import Path


SCRIPT = Path("scripts/bootstrap_storage.sh").read_text(encoding="utf-8")
ENV = Path(".env.example").read_text(encoding="utf-8")


def test_storage_bootstrap_is_fail_closed() -> None:
    assert 'AITOS_DATA_DISK_UUID is required' in SCRIPT
    assert 'mountpoint -q "$DATA_ROOT"' in SCRIPT
    assert 'Mounted source verification failed' in SCRIPT
    assert 'never' not in SCRIPT.lower() or 'never falls back' in SCRIPT.lower()


def test_storage_bootstrap_creates_current_layout() -> None:
    assert '"$DATA_ROOT/clickhouse"' in SCRIPT
    assert '"$DATA_ROOT/neo4j"' in SCRIPT
    assert '"$DATA_ROOT/redis/live"' in SCRIPT
    assert '"$DATA_ROOT/redis/archive"' in SCRIPT


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
