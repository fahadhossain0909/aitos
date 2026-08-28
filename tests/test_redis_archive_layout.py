from pathlib import Path


def test_redis_layout_documented() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "${REDIS_DATA_DIR:-./.storage/data/redis}/live:/data" in compose
    assert "${REDIS_DATA_DIR:-./.storage/data/redis}/archive:/archive" in compose
    assert "redis-archive:/archive" not in compose
