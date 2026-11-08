from aitos.journal.postgres_repository import PostgresJournalRepository


def test_trade_snapshot_retry_key_excludes_generated_recorded_at():
    first = {
        "trade_id": "trade-1",
        "state": "POSITION_OPENED",
        "entry_time": "2026-11-08T10:00:00+00:00",
        "quantity": 0.25,
        "mae_price": 99.5,
        "recorded_at": "2026-11-08T10:00:01+00:00",
    }
    retry = {
        **first,
        "recorded_at": "2026-11-08T10:00:08+00:00",
    }
    first_identity = PostgresJournalRepository._trade_snapshot_identity(first)
    retry_identity = PostgresJournalRepository._trade_snapshot_identity(retry)
    assert first_identity == retry_identity
    assert PostgresJournalRepository._idempotency_key(
        "trade", first_identity
    ) == PostgresJournalRepository._idempotency_key("trade", retry_identity)


def test_trade_snapshot_material_change_gets_new_idempotency_key():
    original = {"trade_id": "trade-1", "state": "POSITION_OPENED", "quantity": 0.25}
    updated = {**original, "mae_price": 98.75}
    assert PostgresJournalRepository._idempotency_key(
        "trade", PostgresJournalRepository._trade_snapshot_identity(original)
    ) != PostgresJournalRepository._idempotency_key(
        "trade", PostgresJournalRepository._trade_snapshot_identity(updated)
    )
