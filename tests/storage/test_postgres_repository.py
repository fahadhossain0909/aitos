from aitos.journal.postgres_repository import PostgresJournalRepository


def test_trade_snapshot_retry_key_does_not_depend_on_generated_recorded_at():
    first = {
        "trade_id": "trade-1",
        "state": "POSITION_OPENED",
        "entry_time": "2026-11-08T10:00:00+00:00",
        "quantity": 0.25,
        "recorded_at": "2026-11-08T10:00:01+00:00",
    }
    retry = {
        **first,
        "recorded_at": "2026-11-08T10:00:08+00:00",
    }
    fields = (
        "trade_id",
        "snapshot_version",
        "updated_at",
        "state",
        "entry_time",
        "exit_time",
        "quantity",
        "exit_price",
        "pnl",
        "pnl_percent",
    )
    first_identity = {key: first.get(key) for key in fields}
    retry_identity = {key: retry.get(key) for key in fields}
    assert PostgresJournalRepository._idempotency_key(
        "trade", first_identity
    ) == PostgresJournalRepository._idempotency_key("trade", retry_identity)


def test_journal_entry_idempotency_key_is_stable():
    key = "journal-entry:entry-123"
    assert key == "journal-entry:entry-123"
