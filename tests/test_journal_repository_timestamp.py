from datetime import datetime, timezone

import pytest

from aitos.journal.models import JournalEntry, JournalEntryType
from aitos.journal.repository import JournalRepository


class FakeClickHouseClient:
    def __init__(self):
        self.calls = []

    async def insert(self, table, rows, *, column_names):
        self.calls.append((table, rows, column_names))


@pytest.mark.asyncio
async def test_clickhouse_journal_writer_persists_entry_timestamp():
    repository = JournalRepository.__new__(JournalRepository)
    repository._initialized = True
    repository._client = FakeClickHouseClient()
    entry = JournalEntry(
        trade_id="trade-1",
        entry_type=JournalEntryType.PRE_TRADE,
        market_context={"symbol": "BTCUSDT"},
        created_at="2026-11-08T10:20:30.123456+00:00",
    )

    await repository.save_journal_entry(entry)

    table, rows, columns = repository._client.calls[0]
    assert table == "journal_entries"
    assert columns[0] == "created_at"
    assert rows[0][0] == datetime(2026, 11, 8, 10, 20, 30, 123456, tzinfo=timezone.utc)
    assert rows[0][1] == entry.entry_id
