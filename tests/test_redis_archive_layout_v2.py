from pathlib import Path


def test_redis_storage_layout() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "${REDIS_DATA_DIR:-${AITOS_DATA_ROOT:-./data}/eventbus/redis}/live:/data" in compose
    assert (
        "${REDIS_DATA_DIR:-${AITOS_DATA_ROOT:-./data}/eventbus/redis}/archive:/archive" in compose
    )
    assert "./data/redis-archive" not in compose
