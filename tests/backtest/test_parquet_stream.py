from datetime import datetime, timedelta, timezone

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq

from aitos.backtest.parquet_stream import ParquetChunkReader
from aitos.backtest.streaming import ChunkPlanner


def test_parquet_reader_prunes_time_window_and_columns(tmp_path):
    table = pa.table(
        {
            "timestamp": [
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:30:00+00:00",
                "2024-01-02T00:00:00+00:00",
            ],
            "price": [100.0, 101.0, 102.0],
            "quantity": [1.0, 2.0, 3.0],
        }
    )
    path = tmp_path / "trades.parquet"
    pq.write_table(table, path)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    reader = ParquetChunkReader(path, columns=["timestamp", "price"])
    planner = ChunkPlanner(start, end, timedelta(hours=12))

    chunks = list(reader.iter_chunks(planner))
    rows = [row for chunk in chunks for row in chunk.events]

    assert len(rows) == 2
    assert set(rows[0]) == {"timestamp", "price"}
    assert {row["price"] for row in rows} == {100.0, 101.0}
