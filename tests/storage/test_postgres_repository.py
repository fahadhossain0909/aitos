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


def test_postgres_jsonb_payload_decoding_handles_asyncpg_default_string():
    payload = PostgresJournalRepository._decode_payload('{"trade_id":"t-1","pnl":1.25}')
    assert payload == {"trade_id": "t-1", "pnl": 1.25}


def test_postgres_outcome_holding_time_handles_utc_timestamps():
    seconds = PostgresJournalRepository._holding_seconds(
        "2026-11-08T10:00:00+00:00", "2026-11-08T10:03:30+00:00"
    )
    assert seconds == 210.0
