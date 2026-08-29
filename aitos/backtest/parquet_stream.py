"""Memory-bounded Parquet reader for historical backtests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .streaming import BacktestChunk, ChunkPlanner

try:
    import pyarrow.dataset as ds
except ImportError:  # pragma: no cover
    ds = None


class ParquetChunkReader:
    """Read only the requested time window and columns from a Parquet dataset.

    Requires pyarrow at runtime. The dataset should contain a ``timestamp``
    column and is preferably partitioned by date/symbol for efficient pruning.
    """

    def __init__(
        self,
        path: str | Path,
        parser: Callable[[dict[str, Any]], Any] | None = None,
        columns: Sequence[str] | None = None,
    ):
        if ds is None:
            raise ImportError("ParquetChunkReader requires pyarrow")
        self.path = Path(path)
        self.parser = parser or (lambda row: row)
        self.columns = list(columns) if columns else None
        self.dataset = ds.dataset(str(self.path), format="parquet", partitioning="hive")

    def iter_chunks(self, planner: ChunkPlanner) -> Iterator[BacktestChunk]:
        for index, (start, end) in enumerate(planner.windows()):
            start_s = start.isoformat()
            end_s = end.isoformat()
            predicate = (ds.field("timestamp") >= start_s) & (
                ds.field("timestamp") < end_s
            )
            table = self.dataset.to_table(filter=predicate, columns=self.columns)
            events = tuple(self.parser(row) for row in table.to_pylist())
            yield BacktestChunk(index, start, end, events)

    def iter_rows(
        self,
        start: datetime,
        end: datetime,
    ) -> Iterator[Any]:
        predicate = (ds.field("timestamp") >= start.isoformat()) & (
            ds.field("timestamp") < end.isoformat()
        )
        scanner = self.dataset.scanner(
            filter=predicate, columns=self.columns, batch_size=50_000
        )
        for batch in scanner.to_batches():
            for row in batch.to_pylist():
                yield self.parser(row)
