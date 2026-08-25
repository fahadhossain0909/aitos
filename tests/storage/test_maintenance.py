from pathlib import Path

from aitos.storage.maintenance import (
    StorageConfig,
    choose_retention_days,
    inspect_boot_storage,
    prune_old_files,
)


def test_retention_ladder_prefers_longest_window_that_fits():
    assert choose_retention_days(120, 90, 0.5) == 90
    assert choose_retention_days(120, 90, 2.0) == 30
    assert choose_retention_days(120, 90, 20.0) == 7


def test_inspect_boot_storage_reports_over_budget(tmp_path: Path):
    big = tmp_path / "blob.bin"
    big.write_bytes(b"x" * 1024)
    config = StorageConfig(others_gb=0.0000005, boot_buffer_gb=10.0)
    result = inspect_boot_storage(tmp_path, config)
    assert result["others_gb"] > 0
    assert result["others_max_gb"] == config.others_gb
    assert result["boot_buffer_gb"] == 10.0
    assert result["others_over_budget"] is True


def test_inspect_boot_storage_under_budget(tmp_path: Path):
    config = StorageConfig(others_gb=22.5, boot_buffer_gb=10.0)
    result = inspect_boot_storage(tmp_path, config)
    assert result["others_gb"] == 0.0
    assert result["others_over_budget"] is False


def test_prune_old_files_removes_oldest_until_budget(tmp_path: Path):
    old = tmp_path / "old.bin"
    new = tmp_path / "new.bin"
    old.write_bytes(b"o" * 800)
    new.write_bytes(b"n" * 800)
    old.touch()
    new.touch()
    import os
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    result = prune_old_files(tmp_path, max_gb=0.000001, delete_percent=2.5)
    assert result["over_budget"] is True
    assert str(old) in result["deleted_files"]
    assert not old.exists()
    assert new.exists()
