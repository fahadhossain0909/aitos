import json
import os
import sqlite3
import subprocess
import sys

import pytest

from aitos.config import durable_files as files


def test_atomic_settings_and_invalid_values(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    files.atomic_write_json(path, {"schema_version": 1})
    assert json.loads(path.read_text()) == {"schema_version": 1}
    with pytest.raises(ValueError):
        files.atomic_write_json(path, {"value": float("nan")})

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(files.StorageUnavailable):
        files.atomic_write_json(path, {"schema_version": 2})
    assert json.loads(path.read_text()) == {"schema_version": 1}
    assert not list(tmp_path.glob(".aitos-atomic-*"))


def test_process_exclusion_and_crash_releases_kernel_lock(tmp_path):
    lock = tmp_path / "writer.lock"
    program = """
import os, sys
from aitos.config.durable_files import exclusive_file_lock, StorageUnavailable
try:
    with exclusive_file_lock(sys.argv[1]):
        os._exit(23)
except StorageUnavailable:
    sys.exit(3)
"""
    with files.exclusive_file_lock(lock):
        assert (
            subprocess.run([sys.executable, "-c", program, str(lock)]).returncode == 3
        )
    assert subprocess.run([sys.executable, "-c", program, str(lock)]).returncode == 23
    with files.exclusive_file_lock(lock):
        assert lock.exists()


def test_online_sqlite_snapshot_and_fresh_restore_candidate(tmp_path):
    source, backup, restored = (
        tmp_path / n for n in ("live.sqlite", "backup.sqlite", "candidate.sqlite")
    )
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE evidence(value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('synthetic')")
        db.commit()
        files.stage_sqlite_snapshot(source, backup)
    files.stage_sqlite_snapshot(backup, restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT value FROM evidence").fetchall() == [("synthetic",)]
    with pytest.raises(files.StorageUnavailable):
        files.stage_sqlite_snapshot(backup, source)
    with pytest.raises(files.StorageUnavailable):
        files.stage_sqlite_snapshot(tmp_path / "missing", tmp_path / "new.sqlite")
    assert not list(tmp_path.glob(".aitos-snapshot-*"))


def test_cache_temporary_and_installation_paths_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "get_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(files, "get_project_root", lambda: tmp_path / "install")
    monkeypatch.setattr(files.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    for parent in ("cache", "install", "temp"):
        with pytest.raises(files.StorageUnavailable):
            files.prepare_durable_directory(tmp_path / parent / "journal")
    result = files.prepare_durable_directory(tmp_path / "durable")
    assert result.is_dir()
    monkeypatch.setattr(
        files.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})()
    )
    with pytest.raises(files.StorageUnavailable, match="disk reserve"):
        files.prepare_durable_directory(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_shared_permissions_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(files.tempfile, "gettempdir", lambda: "/different-temp-root")
    path = tmp_path / "shared"
    path.mkdir(mode=0o777)
    path.chmod(0o777)
    with pytest.raises(files.StorageUnavailable, match="private storage"):
        files.prepare_durable_directory(path)
