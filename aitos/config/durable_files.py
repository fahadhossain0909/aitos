"""Opt-in durable file primitives. No automatic migration, restore or RAM fallback.

Use only for explicitly selected user storage. Existing financial databases are
not relocated by importing this module. Restore candidates never replace a live DB.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from aitos.config.paths import get_cache_dir, get_project_root


class StorageUnavailable(RuntimeError):
    pass


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def prepare_durable_directory(path, *, reserve_bytes=64 * 1024 * 1024):
    """Fail closed on forbidden roots, permissions, disk or write failures."""
    if type(reserve_bytes) is not int or reserve_bytes < 1024 * 1024:
        raise ValueError("positive bounded disk reserve required")
    path = Path(path).expanduser().resolve()
    for forbidden in (get_cache_dir(), get_project_root(), Path(tempfile.gettempdir())):
        if path == forbidden.resolve() or forbidden.resolve() in path.parents:
            raise StorageUnavailable(
                "installation, cache and temporary storage forbidden"
            )
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise StorageUnavailable("private storage permissions required")
        if shutil.disk_usage(path).free < reserve_bytes:
            raise StorageUnavailable("persistent storage disk reserve unavailable")
        fd, probe = tempfile.mkstemp(prefix=".aitos-probe-", dir=path)
        try:
            os.write(fd, b"storage-probe")
            os.fsync(fd)
        finally:
            os.close(fd)
            os.unlink(probe)
        _sync_directory(path)
    except OSError:
        raise StorageUnavailable("persistent storage unavailable") from None
    return path


@contextlib.contextmanager
def exclusive_file_lock(path):
    """Nonblocking OS lock; process death releases it, never unlink a held lock."""
    fd = None
    acquired = False
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError:
        if fd is not None:
            os.close(fd)
        raise StorageUnavailable("storage is locked or unavailable") from None
    try:
        yield
    finally:
        if fd is not None:
            try:
                if acquired and os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                elif acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def atomic_write_json(path, value):
    """Same-directory, fsynced replace; errors propagate even after rename."""
    payload = json.dumps(value, allow_nan=False, ensure_ascii=True).encode("utf-8")
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("bounded settings document required")
    path = Path(path)
    if path.is_symlink():
        raise StorageUnavailable("symlink destination forbidden")
    with exclusive_file_lock(path.with_name(path.name + ".lock")):
        fd, temporary = tempfile.mkstemp(prefix=".aitos-atomic-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        except OSError:
            raise StorageUnavailable(
                "durable write failed; inspect outcome before retry"
            ) from None
        finally:
            Path(temporary).unlink(missing_ok=True)


def _is_windows():
    return os.name == "nt"


def stage_sqlite_snapshot(source, destination, *, timeout_seconds=10):
    """Consistent online backup/staged restore. Never overwrite/activate a DB."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("bounded backup timeout required")
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or destination.exists() or destination.is_symlink():
        raise StorageUnavailable("owned source and fresh destination required")
    with exclusive_file_lock(destination.with_name(destination.name + ".lock")):
        fd, temporary = tempfile.mkstemp(
            prefix=".aitos-snapshot-", dir=destination.parent
        )
        os.close(fd)
        started = time.monotonic()

        def progress(*_):
            if time.monotonic() - started > timeout_seconds:
                raise StorageUnavailable("backup deadline exceeded")

        try:
            uri = source.resolve().as_uri() + "?mode=ro"
            with contextlib.closing(
                sqlite3.connect(uri, uri=True, timeout=3)
            ) as original, contextlib.closing(sqlite3.connect(temporary)) as snapshot:
                original.backup(snapshot, pages=256, progress=progress, sleep=0.05)
                if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StorageUnavailable("backup integrity check failed")
            # Open read/write before fsync: Windows does not reliably flush a
            # read-only descriptor. Keep publication atomic and no-clobber.
            with open(temporary, "r+b") as stream:
                os.fsync(stream.fileno())
            if _is_windows():
                # Windows rename fails if the destination already exists.
                os.rename(temporary, destination)
            else:
                # POSIX hard-link is an atomic no-clobber publish.
                os.link(temporary, destination)
            _sync_directory(destination.parent)
        except (OSError, sqlite3.Error):
            raise StorageUnavailable(
                "snapshot unavailable; no authority changed"
            ) from None
        finally:
            Path(temporary).unlink(missing_ok=True)
    return destination
