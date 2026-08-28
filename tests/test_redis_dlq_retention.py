from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_redis_dlq_retention_worker_is_configured():
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = (ROOT / "scripts" / "redis_dlq_retention.sh").read_text()

    assert "redis-dlq-retention:" in compose
    assert "REDIS_DLQ_MAXLEN: \"25000\"" in compose
    assert "REDIS_DLQ_TRIM_INTERVAL_SECONDS: \"5\"" in compose
    assert "stream:dlq MAXLEN '~' \"$DLQ_MAXLEN\"" in worker
    assert "mem_limit: 64m" in compose


def test_redis_dlq_retention_worker_does_not_delete_other_streams():
    worker = (ROOT / "scripts" / "redis_dlq_retention.sh").read_text()

    assert "XTRIM stream:dlq" in worker
    assert "FLUSHDB" not in worker
    assert "FLUSHALL" not in worker
    assert "DEL " not in worker
