"""Download -> normalize -> canonical Parquet ingestion pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .incremental import DownloadItem, IncrementalDownloader
from .parquet_writer import CanonicalParquetWriter


@dataclass(frozen=True)
class IngestionResult:
    downloaded: tuple[Path, ...]
    written: tuple[Path, ...]


class DatasetIngestionPipeline:
    """Wire dataset-aware downloading directly to canonical Parquet writing."""

    def __init__(
        self,
        downloader: IncrementalDownloader,
        parquet_root: str | Path,
        normalizers: dict[str, Callable[[Path, DownloadItem], Iterable[Any]]],
    ) -> None:
        self.downloader = downloader
        self.normalizers = normalizers
        self.parquet_root = Path(parquet_root)

    def run(
        self, items: Iterable[DownloadItem], overwrite: bool = False
    ) -> IngestionResult:
        requested = list(items)
        downloaded = self.downloader.download_items(requested, overwrite=overwrite)
        by_destination = {item.destination: item for item in requested}
        written: list[Path] = []
        for raw_path in downloaded:
            item = by_destination[raw_path]
            normalizer = self.normalizers.get(item.dataset)
            if normalizer is None:
                raise ValueError(
                    f"No normalizer registered for dataset: {item.dataset}"
                )
            writer = CanonicalParquetWriter(self._dataset_root(item.dataset))
            written.extend(writer.write(normalizer(raw_path, item)))
        return IngestionResult(tuple(downloaded), tuple(written))

    def _dataset_root(self, dataset: str) -> Path:
        mapping = {
            "trades": self.parquet_root / "trades",
            "prices": self.parquet_root / "prices",
            "funding": self.parquet_root / "funding",
            "open_interest": self.parquet_root / "open_interest",
            "liquidations": self.parquet_root / "liquidations",
            "orderbook_snapshots": self.parquet_root / "orderbook" / "snapshots",
            "orderbook_updates": self.parquet_root / "orderbook" / "updates",
        }
        try:
            return mapping[dataset]
        except KeyError as exc:
            raise ValueError(f"Unknown canonical dataset: {dataset}") from exc
