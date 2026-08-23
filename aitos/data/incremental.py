"""Incremental download with dataset-aware canonical Parquet gating."""

from __future__ import annotations

import hashlib
import json
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .dataset_layout import DatasetLayout
from .dataset_policy import DatasetGate


@dataclass(frozen=True)
class FileRecord:
    key: str
    url: str
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DownloadItem:
    """A raw source file mapped to one canonical dataset partition."""

    dataset: str
    exchange: str
    market: str
    symbol: str
    day: date
    url: str
    destination: Path


class DownloadManifest:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        if self.path.exists():
            self.records = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.records, indent=2, sort_keys=True), encoding="utf-8"
        )

    def valid(
        self,
        key: str,
        path: Path,
        expected_size: int | None = None,
        sha256: str | None = None,
    ) -> bool:
        rec = self.records.get(key)
        if not rec or not path.exists():
            return False
        if expected_size is not None and path.stat().st_size != expected_size:
            return False
        if sha256 is not None and rec.get("sha256") != sha256:
            return False
        return rec.get("size") == path.stat().st_size


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_download_url(url: str) -> None:
    scheme = urlparse(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported download URL scheme: {scheme or '<missing>'}")


def _download_to_path(url: str, destination: Path) -> None:
    """Download a validated HTTP(S) URL without enabling local-file schemes."""
    _validate_download_url(url)
    # URL scheme validation above intentionally constrains this urlopen call.
    with urllib.request.urlopen(url, timeout=30) as response:  # nosec B310
        with destination.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)


class CanonicalDataIndex:
    """Backward-compatible index for legacy ``key/url/path`` downloads.

    New dataset-aware callers should use :class:`DatasetGate` through
    ``download_items``.  The legacy tests and callers predate the dataset
    directory prefix, so this index recognizes their historical partition
    layout without weakening the dataset-aware gate.
    """

    def __init__(self, parquet_root: str | Path):
        self.root = Path(parquet_root)

    def has_partition(self, key: str) -> bool:
        try:
            exchange, market, symbol, day = key.split("/", 3)
        except ValueError:
            return False
        partition = (
            self.root
            / f"exchange={exchange}"
            / f"market={market}"
            / f"symbol={symbol.upper()}"
            / f"date={day}"
        )
        return partition.is_dir() and any(partition.glob("*.parquet"))


class IncrementalDownloader:
    """Download only data absent from its own canonical Parquet partition.

    The DatasetGate is the first gate. Therefore an existing trades partition
    cannot suppress an order-book download, and an existing order-book update
    partition cannot suppress a snapshot download.
    """

    def __init__(
        self,
        manifest: DownloadManifest,
        parquet_root: str | Path | CanonicalDataIndex,
    ):
        self.manifest = manifest
        if isinstance(parquet_root, CanonicalDataIndex):
            self.legacy_index = parquet_root
            self.gate = DatasetGate(DatasetLayout(parquet_root.root))
        else:
            self.legacy_index = CanonicalDataIndex(parquet_root)
            self.gate = DatasetGate(DatasetLayout(Path(parquet_root)))

    @staticmethod
    def _manifest_key(item: DownloadItem) -> str:
        return f"{item.dataset}/{item.exchange}/{item.market}/{item.symbol.upper()}/{item.day.isoformat()}"

    def download_items(
        self, items: Iterable[DownloadItem], overwrite: bool = False
    ) -> list[Path]:
        downloaded: list[Path] = []
        for item in items:
            key = self._manifest_key(item)

            if not overwrite and not self.gate.should_download_raw(
                item.dataset, item.exchange, item.market, item.symbol, item.day
            ):
                continue
            if not overwrite and self.manifest.valid(key, item.destination):
                continue

            _validate_download_url(item.url)
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=item.destination.parent, delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
            try:
                _download_to_path(item.url, tmp_path)
                digest = sha256_file(tmp_path)
                size = tmp_path.stat().st_size
                tmp_path.replace(item.destination)
                self.manifest.records[key] = {
                    "dataset": item.dataset,
                    "exchange": item.exchange,
                    "market": item.market,
                    "symbol": item.symbol.upper(),
                    "date": item.day.isoformat(),
                    "url": item.url,
                    "path": str(item.destination),
                    "size": size,
                    "sha256": digest,
                }
                self.manifest.save()
                downloaded.append(item.destination)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        return downloaded

    def download(
        self,
        items: Iterable[tuple[str, str, Path]],
        overwrite: bool = False,
    ) -> list[Path]:
        """Backward-compatible raw download API.

        Legacy callers still use ``key/url/destination``. If the canonical
        partition already exists in the historical layout, no raw download is
        needed; otherwise an existing manifest/raw file still prevents a
        duplicate download.
        """
        downloaded: list[Path] = []
        for key, url, destination in items:
            if not overwrite and self.legacy_index.has_partition(key):
                continue
            if not overwrite and self.manifest.valid(key, destination):
                continue
            _validate_download_url(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
            try:
                _download_to_path(url, tmp_path)
                digest = sha256_file(tmp_path)
                size = tmp_path.stat().st_size
                tmp_path.replace(destination)
                self.manifest.records[key] = {
                    "url": url,
                    "path": str(destination),
                    "size": size,
                    "sha256": digest,
                }
                self.manifest.save()
                downloaded.append(destination)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        return downloaded
