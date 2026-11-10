"""Offline verified SQLite backup bundles. Restore stages, never activates authority.

Invoke only against an explicitly selected database/destination. No discovery,
credential backup, automatic migration, deletion or runtime configuration edits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import contextlib

from aitos.config.durable_files import (
    StorageUnavailable,
    atomic_write_json,
    exclusive_file_lock,
    stage_sqlite_snapshot,
)


def _digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise StorageUnavailable("regular owned backup file required")


def _schema(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def create_backup(source, directory):
    """Publish a completed manifest only after the consistent snapshot is durable.

    Destination must be new. Incomplete directories are deliberately retained
    for inspection, never silently reused or treated as restorable bundles.
    """
    source, directory = Path(source), Path(directory)
    _regular(source)
    if directory.is_symlink():
        raise StorageUnavailable("fresh backup directory required")
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    with exclusive_file_lock(directory / "bundle.lock"):
        snapshot = stage_sqlite_snapshot(source, directory / "database.sqlite3")
        manifest = {
            "schema_version": 1,
            "kind": "aitos-sqlite-staged-backup",
            "file": "database.sqlite3",
            "sha256": _digest(snapshot),
            "bytes": snapshot.stat().st_size,
            "sqlite_user_version": _schema(snapshot),
            "authority": "none",
        }
        atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def stage_restore(directory, destination, *, expected_user_version):
    """Verify bundle, schema and copied bytes before returning a staged candidate.

    Existing destinations are never overwritten. Caller must stop and review
    recovery independently; this API cannot replace an active database.
    Hashes detect corruption, not malicious replacement/authenticity.
    """
    if type(expected_user_version) is not int or expected_user_version < 0:
        raise ValueError("explicit SQLite schema version required")
    directory, destination = Path(directory), Path(destination)
    if directory.is_symlink():
        raise StorageUnavailable("owned backup directory required")
    with exclusive_file_lock(directory / "bundle.lock"):
        manifest_path = directory / "manifest.json"
        _regular(manifest_path)
        if manifest_path.stat().st_size > 4096:
            raise StorageUnavailable("bounded manifest required")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise StorageUnavailable("invalid backup manifest") from None
        if type(manifest) is not dict or set(manifest) != {
            "schema_version",
            "kind",
            "file",
            "sha256",
            "bytes",
            "sqlite_user_version",
            "authority",
        }:
            raise StorageUnavailable("recognized manifest required")
        if (
            type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or manifest["kind"] != "aitos-sqlite-staged-backup"
            or manifest["file"] != "database.sqlite3"
            or manifest["authority"] != "none"
            or type(manifest["sqlite_user_version"]) is not int
            or manifest["sqlite_user_version"] != expected_user_version
        ):
            raise StorageUnavailable("incompatible backup contract")
        snapshot = directory / "database.sqlite3"
        _regular(snapshot)
        if (
            type(manifest["bytes"]) is not int
            or snapshot.stat().st_size != manifest["bytes"]
            or _digest(snapshot) != manifest["sha256"]
            or _schema(snapshot) != expected_user_version
        ):
            raise StorageUnavailable("backup integrity mismatch")
        stage_sqlite_snapshot(snapshot, destination)
        # SQLite backup may change page metadata, so compare semantic schema,
        # not byte hashes of the newly generated candidate.
        if _schema(destination) != expected_user_version:
            raise StorageUnavailable(
                "staged candidate schema mismatch; no authority changed"
            )
    return destination


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Offline SQLite backup/staging; never activates recovery"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--source", required=True)
    backup.add_argument("--destination", required=True)
    restore = commands.add_parser("stage-restore")
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--expected-user-version", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            create_backup(args.source, args.destination)
        else:
            stage_restore(
                args.bundle,
                args.destination,
                expected_user_version=args.expected_user_version,
            )
    except (OSError, ValueError, StorageUnavailable, sqlite3.Error):
        parser.exit(
            2,
            "Backup/staging failed; inspect selected storage. No active database was replaced.\n",
        )
    print("Backup/staging complete. Recovery authority was NOT activated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
