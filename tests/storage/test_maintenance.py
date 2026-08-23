import os
import time
from pathlib import Path

from aitos.storage.maintenance import (choose_retention_days,
                                       enforce_backtest_cache)


def test_retention_ladder_prefers_longest_window_that_fits():
    assert choose_retention_days(120, 90, 0.5) == 90
    assert choose_retention_days(120, 90, 2.0) == 30
    assert choose_retention_days(120, 90, 20.0) == 7


def test_cache_removes_oldest_files_first(tmp_path: Path):
    old = tmp_path / "old.parquet"
    new = tmp_path / "new.parquet"
    manifest = tmp_path / "manifest.json"
    old.write_bytes(b"a" * 10)
    new.write_bytes(b"b" * 10)
    manifest.write_text("{}", encoding="utf-8")

    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    result = enforce_backtest_cache(tmp_path, max_gb=15 / (1024**3))

    assert str(old) in result["removed"]
    assert new.exists()
    assert manifest.exists()
