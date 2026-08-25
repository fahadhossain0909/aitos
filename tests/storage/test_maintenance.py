from pathlib import Path

from aitos.storage.maintenance import (
    StorageConfig,
    choose_retention_days,
    inspect_boot_storage,
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
