import json
import sqlite3

import pytest

from aitos.config.backup import create_backup, stage_restore
from aitos.config.durable_files import StorageUnavailable


def database(tmp_path):
    path = tmp_path / "active.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA user_version=7")
        db.execute("CREATE TABLE ledger(amount TEXT)")
        db.execute("INSERT INTO ledger VALUES ('123.45000001')")
    return path


def test_backup_restore_never_activates_or_overwrites(tmp_path):
    active = database(tmp_path)
    bundle = tmp_path / "backup"
    manifest = create_backup(active, bundle)
    assert manifest["authority"] == "none"
    with sqlite3.connect(active) as db:
        db.execute("INSERT INTO ledger VALUES ('9')")
    candidate = tmp_path / "candidate.sqlite3"
    assert stage_restore(bundle, candidate, expected_user_version=7) == candidate
    with sqlite3.connect(candidate) as db:
        assert db.execute("SELECT amount FROM ledger").fetchall() == [("123.45000001",)]
    with sqlite3.connect(active) as db:
        assert db.execute("SELECT count(*) FROM ledger").fetchone()[0] == 2
    with pytest.raises(StorageUnavailable):
        stage_restore(bundle, active, expected_user_version=7)
    with pytest.raises(FileExistsError):
        create_backup(active, bundle)


@pytest.mark.parametrize(
    "change", ["checksum", "path", "version", "authority", "unknown", "truncated"]
)
def test_corrupt_or_incompatible_bundle_rejected(tmp_path, change):
    bundle = tmp_path / "backup"
    manifest = create_backup(database(tmp_path), bundle)
    path = bundle / "manifest.json"
    if change == "checksum":
        manifest["sha256"] = "0" * 64
    elif change == "path":
        manifest["file"] = "../active.sqlite3"
    elif change == "version":
        manifest["sqlite_user_version"] = 8
    elif change == "authority":
        manifest["authority"] = "live"
    elif change == "unknown":
        manifest["extra"] = True
    if change == "truncated":
        path.write_text("{")
    else:
        path.write_text(json.dumps(manifest))
    candidate = tmp_path / "candidate.sqlite3"
    with pytest.raises(StorageUnavailable):
        stage_restore(bundle, candidate, expected_user_version=7)
    assert not candidate.exists()


def test_incomplete_bundle_not_restorable(tmp_path):
    bundle = tmp_path / "incomplete"
    bundle.mkdir()
    with pytest.raises(StorageUnavailable):
        stage_restore(bundle, tmp_path / "candidate", expected_user_version=7)


def test_schema_and_symlinks_fail_closed(tmp_path):
    bundle = tmp_path / "backup"
    create_backup(database(tmp_path), bundle)
    with pytest.raises(StorageUnavailable):
        stage_restore(bundle, tmp_path / "candidate", expected_user_version=9)
    link = tmp_path / "link"
    try:
        link.symlink_to(bundle, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(StorageUnavailable):
        stage_restore(link, tmp_path / "candidate", expected_user_version=7)


def test_cli_is_explicit_and_does_not_print_sensitive_paths(tmp_path, capsys):
    from aitos.config.backup import main

    active = database(tmp_path)
    bundle = tmp_path / "backup"
    assert main(["backup", "--source", str(active), "--destination", str(bundle)]) == 0
    assert (
        main(
            [
                "stage-restore",
                "--bundle",
                str(bundle),
                "--destination",
                str(tmp_path / "staged"),
                "--expected-user-version",
                "7",
            ]
        )
        == 0
    )
    assert str(tmp_path) not in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(
            [
                "stage-restore",
                "--bundle",
                str(bundle),
                "--destination",
                str(active),
                "--expected-user-version",
                "7",
            ]
        )
    assert error.value.code == 2
    assert str(tmp_path) not in capsys.readouterr().err
