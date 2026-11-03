"""Dataset-aware partitioned Parquet writer with manifest-backed deduplication."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .parquet_manifest import ParquetManifest, PartitionRecord

_DATASET_BY_TYPE = {
    "CanonicalTrade": "trades",
    "CanonicalBookEvent": "orderbook/updates",
    "CanonicalBookSnapshot": "orderbook/snapshots",
}


class CanonicalParquetWriter:
    def __init__(
        self,
        root: str | Path,
        compression: str = "zstd",
        manifest: str | Path | None = None,
    ):
        self.root = Path(root)
        self.compression = compression
        self.manifest = ParquetManifest(manifest or self.root / "_manifest.json")

    @staticmethod
    def _fingerprint(rows: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            rows, default=str, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _dataset(event: Any) -> str:
        explicit = getattr(event, "dataset", None)
        if explicit:
            return str(explicit).strip("/")
        name = type(event).__name__
        return _DATASET_BY_TYPE.get(name, "market_data")

    def write(self, events: Iterable[Any]) -> list[Path]:
        groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = (
            defaultdict(list)
        )
        for event in events:
            ts: datetime = event.timestamp
            dataset = self._dataset(event)
            key = (
                dataset,
                event.exchange,
                event.market,
                event.symbol,
                ts.date().isoformat(),
            )
            row = dict(event.__dict__)
            row["timestamp"] = ts
            row["dataset"] = dataset
            groups[key].append(row)

        written: list[Path] = []
        for (dataset, exchange, market, symbol, day), rows in groups.items():
            partition_key = f"{dataset}/{exchange}/{market}/{symbol}/{day}"
            fingerprint = self._fingerprint(rows)
            if self.manifest.contains(partition_key, fingerprint):
                continue

            directory = (
                self.root
                / dataset
                / f"exchange={exchange}"
                / f"market={market}"
                / f"symbol={symbol.upper()}"
                / f"date={day}"
            )
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "part-000000.parquet"
            temp = directory / ".part-000000.tmp.parquet"
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, temp, compression=self.compression)
            temp.replace(target)
            self.manifest.record(
                PartitionRecord(partition_key, str(target), len(rows), fingerprint)
            )
            written.append(target)
        return written
