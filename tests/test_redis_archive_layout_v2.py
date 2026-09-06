from pathlib import Path


def test_redis_storage_layout() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "${REDIS_DATA_DIR:-/mnt/aitos-data/eventbus/redis}/live:/data" in compose
    assert (
        "${REDIS_DATA_DIR:-/mnt/aitos-data/eventbus/redis}/archive:/archive" in compose
    )
    assert "/mnt/aitos-data/redis-archive" not in compose
