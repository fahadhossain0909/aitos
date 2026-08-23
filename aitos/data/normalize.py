"""CSV/JSONL to compressed Parquet normalization utilities."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


def normalize_csv_to_parquet(
    source: str | Path, destination: str | Path, columns: Sequence[str] | None = None
) -> Path:
    try:
        import pyarrow.csv as pv
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError("normalize_csv_to_parquet requires pyarrow") from exc
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    table = pv.read_csv(source, read_options=pv.ReadOptions(use_threads=True))
    if columns:
        table = table.select(list(columns))
    pq.write_table(table, destination, compression="zstd")
    return destination
