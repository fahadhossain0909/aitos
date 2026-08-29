from pathlib import Path

import pytest

from aitos.data.storage_policy import StorageLifecycle


def test_cleanup_refuses_when_no_parquet(tmp_path: Path):
    archive = tmp_path / "raw.zip"
    archive.write_bytes(b"raw")
    extracted = tmp_path / "csv"
    extracted.mkdir()
    (extracted / "data.csv").write_text("x")

    lifecycle = StorageLifecycle()
    with pytest.raises(RuntimeError):
        lifecycle.cleanup(archive, extracted, tmp_path / "parquet")

    assert archive.exists()
    assert extracted.exists()


def test_cleanup_removes_intermediate_data_after_parquet(tmp_path: Path):
    archive = tmp_path / "raw.zip"
    archive.write_bytes(b"raw")
    extracted = tmp_path / "csv"
    extracted.mkdir()
    (extracted / "data.csv").write_text("x")
    parquet = tmp_path / "parquet"
    parquet.mkdir()
    (parquet / "part.parquet").write_bytes(b"parquet")

    removed = StorageLifecycle().cleanup(archive, extracted, parquet)

    assert not archive.exists()
    assert not extracted.exists()
    assert len(removed) == 2
    assert (parquet / "part.parquet").exists()
