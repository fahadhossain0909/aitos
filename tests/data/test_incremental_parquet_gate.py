from pathlib import Path

from aitos.data.incremental import (CanonicalDataIndex, DownloadManifest,
                                    IncrementalDownloader)


def test_existing_parquet_skips_raw_download(tmp_path: Path):
    parquet_root = tmp_path / "parquet"
    partition = (
        parquet_root
        / "exchange=binance"
        / "market=futures_um"
        / "symbol=BTCUSDT"
        / "date=2026-01-01"
    )
    partition.mkdir(parents=True)
    (partition / "part-000000.parquet").write_bytes(b"canonical")

    raw = tmp_path / "raw.zip"
    downloader = IncrementalDownloader(
        DownloadManifest(tmp_path / "manifest.json"),
        CanonicalDataIndex(parquet_root),
    )

    downloaded = downloader.download(
        [
            (
                "binance/futures_um/BTCUSDT/2026-01-01",
                "https://invalid.example/raw.zip",
                raw,
            )
        ]
    )

    assert downloaded == []
    assert not raw.exists()


def test_missing_parquet_still_uses_existing_raw(tmp_path: Path):
    raw = tmp_path / "raw.zip"
    raw.write_bytes(b"raw")
    manifest = DownloadManifest(tmp_path / "manifest.json")
    manifest.records["binance/futures_um/BTCUSDT/2026-01-02"] = {
        "url": "unused",
        "path": str(raw),
        "size": 3,
        "sha256": "unused",
    }
    manifest.save()

    downloader = IncrementalDownloader(
        manifest, CanonicalDataIndex(tmp_path / "parquet")
    )
    assert (
        downloader.download(
            [
                (
                    "binance/futures_um/BTCUSDT/2026-01-02",
                    "https://invalid.example/raw.zip",
                    raw,
                )
            ]
        )
        == []
    )
