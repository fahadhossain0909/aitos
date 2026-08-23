from datetime import datetime, timezone

from aitos.data.parquet_writer import CanonicalParquetWriter
from aitos.data.schema import CanonicalTrade


def test_partitioned_zstd_writer(tmp_path):
    events = [
        CanonicalTrade(
            "binance",
            "futures_um",
            "BTCUSDT",
            "1",
            datetime(2024, 1, 2, tzinfo=timezone.utc),
            42000.0,
            0.1,
            "buy",
        ),
        CanonicalTrade(
            "binance",
            "futures_um",
            "BTCUSDT",
            "2",
            datetime(2024, 1, 2, 0, 0, 1, tzinfo=timezone.utc),
            42001.0,
            0.2,
            "sell",
        ),
    ]
    files = CanonicalParquetWriter(tmp_path).write(events)
    assert len(files) == 1
    assert files[0].exists()
    assert "exchange=binance" in str(files[0])
    assert "date=2024-01-02" in str(files[0])
