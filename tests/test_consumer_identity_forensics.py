from pathlib import Path

from scripts.analyze_consumer_identity_forensics import load_consumers, runtime


def test_load_consumers_reads_redis_json(tmp_path: Path):
    path = tmp_path / "consumers.json"
    path.write_text('[{"name":"persistence-instance-a","pending":0}]')

    assert load_consumers(path) == {"persistence-instance-a"}


def test_runtime_reads_restart_count_and_start_time(tmp_path: Path):
    path = tmp_path / "redis_runtime.txt"
    path.write_text("3 2026-09-10T10:00:00Z")

    assert runtime(path) == (3, "2026-09-10T10:00:00Z")
