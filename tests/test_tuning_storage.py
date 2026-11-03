from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from aitos.data.repository import MarketDataRepository


def test_clickhouse_repository_reads_storage_tuning(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_STORAGE_CLICKHOUSE_BATCH_SIZE", "2")
    monkeypatch.setenv("AITOS_TUNING_STORAGE_CLICKHOUSE_QUERY_TIMEOUT_SECONDS", "7")
    repository = MarketDataRepository()
    assert repository._batch_size == 2
    assert repository._query_timeout_seconds == 7


@pytest.mark.asyncio
async def test_clickhouse_bulk_insert_is_split_into_configured_batches():
    repository = MarketDataRepository(batch_size=2, query_timeout_seconds=5)
    repository._initialized = True
    repository._client = AsyncMock()

    await repository._insert_rows(
        "trade_ticks",
        [[1], [2], [3], [4], [5]],
        column_names=["value"],
    )

    assert repository._client.insert.await_count == 3
    assert repository._client.insert.await_args_list == [
        call(
            "trade_ticks",
            [[1], [2]],
            column_names=["value"],
            settings={"async_insert": 1, "wait_for_async_insert": 1},
        ),
        call(
            "trade_ticks",
            [[3], [4]],
            column_names=["value"],
            settings={"async_insert": 1, "wait_for_async_insert": 1},
        ),
        call(
            "trade_ticks",
            [[5]],
            column_names=["value"],
            settings={"async_insert": 1, "wait_for_async_insert": 1},
        ),
    ]
