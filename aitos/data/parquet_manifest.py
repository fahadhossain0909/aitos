"""Manifest and deduplication index for normalized Parquet partitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PartitionRecord:
    key: str
    path: str
    row_count: int
    sha256: str


class ParquetManifest:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        if self.path.exists():
            self.records = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.records, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    def contains(self, key: str, sha256: str | None = None) -> bool:
        record = self.records.get(key)
        if not record:
            return False
        if sha256 is None:
            return True
        return record.get("sha256") == sha256

    def record(self, item: PartitionRecord) -> None:
        self.records[item.key] = asdict(item)
        self.save()


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()
