"""Incremental public-data acquisition pipeline."""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path
from zipfile import ZipFile

from .catalog import RemoteFile, binance_um_daily_aggtrades, bybit_spot_daily_trades
from .incremental import DownloadManifest, IncrementalDownloader


def plan(exchange: str, symbol: str, start, end) -> list[RemoteFile]:
    if exchange == "binance":
        return binance_um_daily_aggtrades(symbol, start, end)
    if exchange == "bybit":
        return bybit_spot_daily_trades(symbol, start, end)
    raise ValueError(f"unsupported exchange: {exchange}")


def download_and_extract(
    items: list[RemoteFile], root: str | Path, manifest_path: str | Path
) -> list[Path]:
    root = Path(root)
    manifest = DownloadManifest(manifest_path)
    downloader = IncrementalDownloader(manifest)
    downloads = downloader.download(
        
            (
                item.filename,
                item.url,
                root
                / "raw"
                / item.exchange
                / item.market
                / item.symbol
                / item.filename,
            )
            for item in items
        
    )
    extracted: list[Path] = []
    for archive in downloads:
        out_dir = archive.parent / "extracted"
        out_dir.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with ZipFile(archive) as zf:
                zf.extractall(out_dir)
                extracted.extend(
                    out_dir / name for name in zf.namelist() if not name.endswith("/")
                )
        elif archive.suffix == ".gz":
            destination = out_dir / archive.with_suffix("").name
            with gzip.open(archive, "rb") as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(destination)
    return extracted
