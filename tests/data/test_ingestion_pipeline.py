from datetime import date, datetime, timezone
from pathlib import Path

from aitos.data.incremental import (DownloadItem, DownloadManifest,
                                    IncrementalDownloader)
from aitos.data.ingestion_pipeline import DatasetIngestionPipeline
from aitos.data.schema import CanonicalTrade


def test_pipeline_writes_canonical_partition(tmp_path: Path):
    raw = tmp_path / "raw.csv"
    raw.write_text("x", encoding="utf-8")
    parquet_root = tmp_path / "normalized"
    manifest = DownloadManifest(tmp_path / "manifest.json")
    downloader = IncrementalDownloader(manifest, parquet_root)

    item = DownloadItem(
        "trades",
        "binance",
        "futures_um",
        "BTCUSDT",
        date(2026, 1, 1),
        raw.as_uri(),
        tmp_path / "download.csv",
    )

    def normalize(path, item):
        yield CanonicalTrade(
            "binance",
            "futures_um",
            "BTCUSDT",
            "1",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            95000.0,
            0.1,
            "buy",
        )

    result = DatasetIngestionPipeline(
        downloader, parquet_root, {"trades": normalize}
    ).run([item])
    assert result.downloaded
    assert result.written
    assert result.written[0].exists()


def test_existing_partition_skips_raw_download(tmp_path: Path):
    parquet_root = tmp_path / "normalized"
    target = (
        parquet_root
        / "trades"
        / "exchange=binance"
        / "market=futures_um"
        / "symbol=BTCUSDT"
        / "date=2026-01-01"
    )
    target.mkdir(parents=True)
    (target / "part-000000.parquet").write_bytes(b"already canonical")

    manifest = DownloadManifest(tmp_path / "manifest.json")
    downloader = IncrementalDownloader(manifest, parquet_root)
    item = DownloadItem(
        "trades",
        "binance",
        "futures_um",
        "BTCUSDT",
        date(2026, 1, 1),
        "https://invalid.example/raw",
        tmp_path / "raw.csv",
    )

    result = DatasetIngestionPipeline(downloader, parquet_root, {}).run([item])
    assert result.downloaded == ()
    assert result.written == ()
