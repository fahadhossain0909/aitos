from pathlib import Path

import aitos.storage.maintenance as maintenance
from aitos.storage.maintenance import (
    StorageConfig,
    choose_retention_days,
    inspect_boot_storage,
    prune_for_boot_buffer,
)


def test_retention_ladder_prefers_longest_window_that_fits():
    assert choose_retention_days(120, 90, 0.5) == 90
    assert choose_retention_days(120, 90, 2.0) == 30
    assert choose_retention_days(120, 90, 20.0) == 7


def test_prune_for_boot_buffer_removes_oldest_disposable_files(monkeypatch, tmp_path: Path):
    disposable = tmp_path / "backtest"
    disposable.mkdir()
    old = disposable / "old.bin"
    new = disposable / "new.bin"
    old.write_bytes(b"o" * 800)
    new.write_bytes(b"n" * 800)

    calls = iter([0, 2 * 1024**3])
    monkeypatch.setattr(maintenance, "_boot_free_bytes", lambda _root: next(calls))

    # The reserve is intentionally smaller than the oldest file so one file
    # is sufficient. This verifies that cleanup stops after the oldest
    # candidate and does not remove newer disposable data unnecessarily.
    result = prune_for_boot_buffer(
        tmp_path,
        free_buffer_gb=0.0000001,
        delete_percent=2.5,
    )

    assert result["cleanup_needed"] is True
    assert result["reserve_met"] is True
    assert str(old) in result["deleted_files"]
    assert not old.exists()
    assert new.exists()
    assert result["deleted_files"] == [str(old)]


def test_inspect_boot_storage_triggers_cleanup_when_reserve_is_breached(
    monkeypatch, tmp_path: Path
):
    disposable = tmp_path / "backtest"
    disposable.mkdir()
    old = disposable / "old.bin"
    old.write_bytes(b"o" * 1024)

    calls = iter([0, 20 * 1024**3])
    monkeypatch.setattr(maintenance, "_boot_free_bytes", lambda _root: next(calls))
    config = StorageConfig(boot_buffer_gb=10.0)

    result = inspect_boot_storage(tmp_path, config)

    assert result["boot_free_gb"] == 0.0
    assert result["boot_buffer_gb"] == 10.0
    assert result["reserve_met"] is False
    assert result["prune"]["cleanup_needed"] is True
    assert result["prune"]["reserve_met"] is True
    assert result["prune"]["deleted_files"] == [str(old)]
    assert not old.exists()


def test_inspect_boot_storage_does_not_cleanup_when_reserve_is_met(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(maintenance, "_boot_free_bytes", lambda _root: 20 * 1024**3)
    config = StorageConfig(boot_buffer_gb=10.0)

    result = inspect_boot_storage(tmp_path, config)

    assert result["boot_free_gb"] == 20.0
    assert result["reserve_met"] is True
    assert result["prune"] is None
